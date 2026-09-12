"""Unit tests for the market-sweep confidence score."""

import pytest

from price_monitor.search.confidence import Candidate, score
from price_monitor.search.detections import select

CITED = "https://argos.co.uk/widget"
# Gemini cites a site by name, not by the product URL the model quotes.
CITED_SITE = "argos.co.uk"


def _score(candidates: list[Candidate], cited_sites: list[str]) -> tuple[float, str]:
    """Score a sweep the way market.py does.

    The winner is chosen by `select`, not re-derived here — that coupling is
    the point: `score` no longer picks its own winner, so the test must feed
    it the same candidate the sheet would record.
    """
    _, cheapest, _ = select(candidates, 0.05)
    return score(candidates, cheapest, cited_sites)


def _candidate(price: float, **overrides) -> Candidate:
    fields = {
        "supplier": "Argos",
        "price": price,
        "url": CITED,
        "in_stock": True,
        "rating": 4.4,
        "rating_count": 900,
        "rating_source": "trustpilot",
    }
    fields.update(overrides)
    return Candidate(**fields)


def _clustered(n: int, **winner_overrides) -> list[Candidate]:
    """A winner at 100 plus n-1 others at 105, 110, 115 ... (ratio ~0.91)."""
    others = [
        _candidate(105.0 + 5 * i, url=f"https://other{i}.co.uk/w") for i in range(n - 1)
    ]
    return [_candidate(100.0, **winner_overrides)] + others


@pytest.mark.parametrize(
    "candidates, expected",
    [
        (_clustered(4), 1.00),
        (_clustered(4, rating=None, rating_count=None), 1.00),
        (_clustered(4, rating=1.4, rating_count=33904), 1.00),
        (_clustered(2), 0.67),
        (_clustered(1), 0.33),
    ],
)
def test_scores_depend_on_the_sweep_not_the_retailer(candidates, expected):
    assert _score(candidates, [CITED_SITE])[0] == expected


def test_a_poorly_rated_retailer_no_longer_lowers_confidence():
    """Rating is shown against each detection; scoring it too would double-count.

    Amazon UK sits at 1.4 on tens of thousands of Trustpilot reviews — a
    retailer nobody would call untrustworthy. The rating cannot separate
    'large retailer people complain about' from 'shop to avoid', so it is
    displayed rather than scored.
    """
    good = _score(_clustered(4, rating=4.8), [CITED_SITE])[0]
    bad = _score(_clustered(4, rating=1.4, rating_count=33904), [CITED_SITE])[0]
    assert good == bad == 1.00


def test_score_never_leaves_the_unit_interval():
    for rating in (0.0, 1.0, 2.0, 3.0, 4.0, 5.0, None):
        value = _score(_clustered(4, rating=rating, rating_count=900), [CITED_SITE])[0]
        assert 0.0 <= value <= 1.0


def test_the_reason_no_longer_mentions_a_rating():
    _, reason = _score(_clustered(4), [CITED_SITE])
    assert "rated" not in reason
    assert "4 sources" in reason


def test_a_winner_well_under_the_others_median_is_punished():
    # Winner 100 against others at 200/220/240 -> median 220, ratio 100/220 =
    # 0.4545. The ratio is never rounded: the outlier factor is a continuous
    # slide, and only the final product is rounded, once.
    others = [_candidate(p, url=f"https://o{p}.co.uk/w") for p in (200.0, 220.0, 240.0)]
    assert _score([_candidate(100.0)] + others, [CITED_SITE])[0] == 0.14


def test_a_modest_gap_under_the_median_is_only_partly_punished():
    # Winner 100 against others at 140/143/146 -> median 143, ratio 0.70.
    others = [_candidate(p, url=f"https://o{p}.co.uk/w") for p in (140.0, 143.0, 146.0)]
    assert _score([_candidate(100.0)] + others, [CITED_SITE])[0] == 0.75


def test_a_sole_source_is_not_punished_twice():
    """n=1 has nothing to compare against, so only corroboration penalises it."""
    assert _score([_candidate(100.0)], [CITED_SITE])[0] == 0.33


def test_an_uncited_url_scores_below_a_cited_one():
    cited = _score(_clustered(4), [CITED_SITE])[0]
    uncited = _score(_clustered(4), ["elsewhere.example"])[0]
    assert uncited < cited
    assert uncited == 0.6


def test_a_winner_with_no_url_scores_lowest_on_sourcing():
    assert _score(_clustered(4, url=""), [CITED_SITE])[0] == 0.3


def test_the_reason_names_every_factor():
    _, reason = _score(_clustered(4), [CITED_SITE])
    assert "4 sources" in reason
    assert "cited" in reason


def test_an_empty_candidate_set_scores_zero():
    assert _score([], [CITED_SITE]) == (0.0, "no candidates")


def test_rating_label_renders_for_the_sheet():
    assert _candidate(100.0).rating_label == "4.4 Trustpilot"
    assert _candidate(100.0, rating=None, rating_count=None).rating_label == ""
    assert _candidate(100.0, rating=4.4, rating_count=12).rating_label == ""


def test_the_scored_winner_is_the_one_the_sheet_records():
    """On a price tie `select` prefers the better-rated retailer.

    `score` used to re-derive the winner with a plain `min` on price, so a tie
    could have it explain the sourcing factor for a different retailer than the
    one written to the sheet. It is handed the winner now, so they cannot
    disagree.
    """
    thin = _candidate(100.0, url="https://thin.co.uk/w", rating=4.9, rating_count=12)
    trusted = _candidate(100.0, url="https://trusted.co.uk/w")
    _, cheapest, _ = select([thin, trusted], 0.05)
    assert cheapest is trusted

    _, reason = score([thin, trusted], cheapest, ["trusted.co.uk"])
    assert "cited" in reason and "uncited" not in reason


def test_a_citation_matches_on_the_retailer_not_the_exact_url():
    """Observed in production: every live sweep scored "uncited url".

    The model quotes a product page in its prose; the grounding metadata cites
    whatever it actually fetched. Exact-string matching therefore failed on
    pages that had genuinely been read, capping every sweep's sourcing factor
    at 0.6 and — at the default threshold — silencing any item with fewer than
    three retailers. Each URL below is the same retailer as the citation.
    """
    for quoted in (
        "https://www.argos.co.uk/product/1234567",
        "https://argos.co.uk/product/1234567/",
        "https://argos.co.uk/product/1234567?clickSR=slp",
    ):
        winner = _candidate(100.0, url=quoted)
        _, reason = score([winner], winner, [CITED_SITE])
        assert reason.endswith("cited"), quoted
        assert "uncited" not in reason


def test_a_different_retailer_is_still_uncited():
    winner = _candidate(100.0, url="https://elsewhere.example/p")
    _, reason = score([winner], winner, [CITED_SITE])
    assert "uncited url" in reason


def test_a_citation_by_bare_brand_name_still_counts():
    """Gemini fills a chunk's title with the site's name, which is sometimes
    only the first label — "Argos" for argos.co.uk."""
    winner = _candidate(100.0, url="https://www.argos.co.uk/product/123")
    _, reason = score([winner], winner, ["argos"])
    assert reason.endswith("cited")


def test_a_redirect_uri_never_counts_as_a_citation():
    """The live failure: Gemini's groundingChunks carry Vertex redirect links,
    which resemble no retailer. Matching against those is what pinned every
    sweep's sourcing factor at 0.6."""
    winner = _candidate(100.0, url="https://argos.co.uk/product/123")
    redirect = "vertexaisearch.cloud.google.com"
    _, reason = score([winner], winner, [redirect])
    assert "uncited url" in reason
