"""Unit tests for selecting a sweep's three answers."""

from price_monitor.search.confidence import Candidate
from price_monitor.search.detections import select, to_detection

PREMIUM = 0.05


def _candidate(price, rating=4.0, count=900, supplier="Shop", url=None):
    return Candidate(
        supplier=supplier,
        price=price,
        url=url or f"https://{supplier.lower()}.co.uk/w",
        in_stock=True,
        rating=rating,
        rating_count=count,
        rating_source="trustpilot",
    )


def test_no_candidates_yields_no_detections():
    assert select([], PREMIUM) == (None, None, None)


def test_cheapest_is_the_lowest_price():
    cands = [_candidate(120.0, supplier="A"), _candidate(100.0, supplier="B")]
    _, cheapest, _ = select(cands, PREMIUM)
    assert cheapest.price == 100.0
    assert cheapest.url == "https://b.co.uk/w"


def test_best_rated_is_the_highest_rating_not_the_lowest_price():
    cands = [
        _candidate(100.0, rating=3.2, supplier="A"),
        _candidate(180.0, rating=4.8, supplier="B"),
    ]
    best_rated, cheapest, _ = select(cands, PREMIUM)
    assert best_rated.price == 180.0
    assert best_rated.rating_label == "4.8 Trustpilot"
    assert cheapest.price == 100.0


def test_best_rated_ties_break_toward_the_lower_price():
    cands = [
        _candidate(150.0, rating=4.5, supplier="A"),
        _candidate(120.0, rating=4.5, supplier="B"),
    ]
    best_rated, _, _ = select(cands, PREMIUM)
    assert best_rated.price == 120.0


def test_cheapest_ties_break_toward_the_higher_rating():
    cands = [
        _candidate(100.0, rating=3.0, supplier="A"),
        _candidate(100.0, rating=4.6, supplier="B"),
    ]
    _, cheapest, _ = select(cands, PREMIUM)
    assert cheapest.rating_label == "4.6 Trustpilot"


def test_cheapest_ties_break_toward_well_reviewed_rating():
    """Even in a price tie, prefer a rating resting on 50+ reviews over thin ones.

    This respects .rated throughout the module: a 4.9 from 12 reviews is noise
    and should not influence the choice when a 4.0 from 900 reviews is available
    at the same price.
    """
    cands = [
        _candidate(100.0, rating=4.9, count=12, supplier="Thin"),
        _candidate(100.0, rating=4.0, count=900, supplier="Trusted"),
    ]
    _, cheapest, _ = select(cands, PREMIUM)
    assert cheapest.supplier == "Trusted"
    assert cheapest.rating_label == "4.0 Trustpilot"


def test_an_unrated_retailer_cannot_be_the_best_rated():
    """No rating is not a rating; ranking it would invent evidence."""
    cands = [
        _candidate(100.0, rating=None, count=None, supplier="A"),
        _candidate(150.0, rating=4.0, supplier="B"),
    ]
    best_rated, cheapest, _ = select(cands, PREMIUM)
    assert best_rated.price == 150.0
    assert cheapest.price == 100.0
    assert cheapest.rating_label == ""


def test_select_returns_candidates_by_identity():
    """The caller needs to recover winners by identity, not by value matching.

    Two candidates can share every field value, so matching a winner back by
    price and domain would pick the wrong one's VAT note. This test pins that
    the function returns the actual Candidate objects from the input list.
    """
    cand_a = _candidate(100.0, rating=3.0, supplier="Same")
    cand_b = _candidate(100.0, rating=3.0, supplier="Same")
    cands = [cand_a, cand_b]
    _, cheapest, _ = select(cands, PREMIUM)
    assert cheapest is cand_a


def test_nothing_rated_leaves_best_rated_and_best_value_empty():
    cands = [
        _candidate(100.0, rating=None, count=None),
        _candidate(120.0, rating=None, count=None),
    ]
    best_rated, cheapest, best_value = select(cands, PREMIUM)
    assert best_rated is None
    assert best_value is None
    assert cheapest.price == 100.0


def test_a_thinly_reviewed_rating_does_not_count_as_rated():
    """4.9 from 12 reviews is noise — ineligible, exactly as unrated is."""
    cands = [_candidate(100.0, rating=4.9, count=12, supplier="A")]
    best_rated, cheapest, best_value = select(cands, PREMIUM)
    assert best_rated is None
    assert best_value is None
    assert cheapest.price == 100.0


def test_best_value_takes_an_upgrade_inside_the_budget():
    # Baseline 100 at 3.0. B is +1.0 rating, earning 1.0 * 5% * 100 = 5.00.
    # B costs +4.00, inside budget.
    cands = [
        _candidate(100.0, rating=3.0, supplier="A"),
        _candidate(104.0, rating=4.0, supplier="B"),
    ]
    _, _, best_value = select(cands, PREMIUM)
    assert best_value.price == 104.0


def test_best_value_refuses_an_upgrade_outside_the_budget():
    # C is +1.5 rating, earning 1.5 * 5% * 100 = 7.50, but costs +18.00.
    cands = [
        _candidate(100.0, rating=3.0, supplier="A"),
        _candidate(118.0, rating=4.5, supplier="C"),
    ]
    _, _, best_value = select(cands, PREMIUM)
    assert best_value.price == 100.0


def test_best_value_falls_back_to_the_baseline_when_nothing_qualifies():
    """'Nothing better-rated is worth the money' is an answer, not a failure."""
    cands = [
        _candidate(100.0, rating=3.0, supplier="A"),
        _candidate(500.0, rating=5.0, supplier="B"),
    ]
    _, _, best_value = select(cands, PREMIUM)
    assert best_value.price == 100.0


def test_best_value_prefers_higher_rating_over_cheaper_unrated_price():
    """When the baseline is better-rated than a cheaper option, stay with baseline.

    Cheapest is 99@2.0 (unrated because count < 50), but baseline is 100@3.0.
    Since nothing is better-rated than the baseline, best_value returns the
    baseline (100), not the cheaper unrated option.
    """
    cands = [
        _candidate(100.0, rating=3.0, count=50, supplier="A"),
        _candidate(99.0, rating=2.0, count=30, supplier="B"),
    ]
    _, cheapest, best_value = select(cands, PREMIUM)
    assert cheapest.price == 99.0
    assert best_value.price == 100.0


def test_a_single_rated_candidate_is_its_own_best_value():
    cands = [_candidate(100.0, rating=4.2, supplier="A")]
    _, _, best_value = select(cands, PREMIUM)
    assert best_value.price == 100.0


def test_best_value_baseline_ignores_an_unrated_cheaper_retailer():
    """The baseline is the cheapest *rated* candidate.

    An unrated baseline has no rating to improve on, and scoring it as zero
    would make every rated shop look like a bargain worth a large premium.
    So value_price can legitimately exceed cheapest_price here.
    """
    cands = [
        _candidate(50.0, rating=None, count=None, supplier="Unknown"),
        _candidate(100.0, rating=3.0, supplier="A"),
        _candidate(104.0, rating=4.0, supplier="B"),
    ]
    _, cheapest, best_value = select(cands, PREMIUM)
    assert cheapest.price == 50.0
    assert best_value.price == 104.0


def test_to_detection_converts_a_winner_for_the_sheet():
    detection = to_detection(_candidate(100.0, rating=4.4, supplier="Argos"))
    assert detection.price == 100.0
    assert detection.site == "argos.co.uk"
    assert detection.rating == "4.4 Trustpilot"


def test_to_detection_passes_none_through():
    assert to_detection(None) is None


def test_best_value_ties_break_toward_the_cheaper():
    cands = [
        _candidate(100.0, rating=3.0, supplier="A"),
        _candidate(104.0, rating=4.0, supplier="C"),
        _candidate(103.0, rating=4.0, supplier="B"),
    ]
    _, _, best_value = select(cands, PREMIUM)
    assert best_value.price == 103.0
