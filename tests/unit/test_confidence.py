"""Unit tests for the market-sweep confidence score."""

import pytest

from price_monitor.search.confidence import Candidate, score

CITED = "https://argos.co.uk/widget"


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
        (_clustered(4, rating=None, rating_count=None), 0.80),
        (_clustered(4, rating=3.2, rating_count=900), 0.44),
        (_clustered(2, rating=None, rating_count=None), 0.53),
        (_clustered(1), 0.33),
    ],
)
def test_scores_match_the_specified_worked_examples(candidates, expected):
    assert score(candidates, [CITED])[0] == expected


def test_a_winner_well_under_the_others_median_is_punished():
    # Winner 100 against others at 200/220/240 -> median 220, ratio 100/220 =
    # 0.4545. The ratio is never rounded: the outlier factor is a continuous
    # slide, and only the final product is rounded, once.
    others = [_candidate(p, url=f"https://o{p}.co.uk/w") for p in (200.0, 220.0, 240.0)]
    assert score([_candidate(100.0)] + others, [CITED])[0] == 0.14


def test_a_modest_gap_under_the_median_is_only_partly_punished():
    # Winner 100 against others at 140/143/146 -> median 143, ratio 0.70.
    others = [_candidate(p, url=f"https://o{p}.co.uk/w") for p in (140.0, 143.0, 146.0)]
    assert score([_candidate(100.0)] + others, [CITED])[0] == 0.75


def test_a_sole_source_is_not_punished_twice():
    """n=1 has nothing to compare against, so only corroboration penalises it."""
    assert score([_candidate(100.0)], [CITED])[0] == 0.33


def test_an_uncited_url_scores_below_a_cited_one():
    cited = score(_clustered(4), [CITED])[0]
    uncited = score(_clustered(4), ["https://elsewhere.example/x"])[0]
    assert uncited < cited
    assert uncited == 0.6


def test_a_winner_with_no_url_scores_lowest_on_sourcing():
    assert score(_clustered(4, url=""), [CITED])[0] == 0.3


def test_the_reputation_shoulder_is_flat_at_and_above_four():
    assert score(_clustered(4, rating=4.0), [CITED])[0] == 1.00
    assert score(_clustered(4, rating=4.9), [CITED])[0] == 1.00


def test_the_reputation_factor_bottoms_out_at_three():
    assert score(_clustered(4, rating=3.0), [CITED])[0] == 0.30


def test_a_rating_on_too_few_reviews_reads_as_unknown():
    """4.9 from 12 reviews is noise, and must not score better than unrated."""
    thin = score(_clustered(4, rating=4.9, rating_count=12), [CITED])[0]
    unrated = score(_clustered(4, rating=None, rating_count=None), [CITED])[0]
    assert thin == unrated == 0.80


def test_the_reason_names_every_factor():
    _, reason = score(_clustered(4), [CITED])
    assert "4 sources" in reason
    assert "cited" in reason
    assert "rated 4.4" in reason


def test_an_empty_candidate_set_scores_zero():
    assert score([], [CITED]) == (0.0, "no candidates")


def test_rating_label_renders_for_the_sheet():
    assert _candidate(100.0).rating_label == "4.4 Trustpilot"
    assert _candidate(100.0, rating=None, rating_count=None).rating_label == ""
    assert _candidate(100.0, rating=4.4, rating_count=12).rating_label == ""
