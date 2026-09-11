"""Unit tests for price_monitor.models value objects."""

from datetime import datetime

import pytest

from price_monitor.models import Detection, Item, PriceReading, PriceStats, PriceStatus


def test_an_item_is_just_a_name():
    """The user types a product, not a shop: the sweep decides where to look."""
    assert Item(name="Widget").name == "Widget"


def test_status_serialises_as_its_string_value():
    # Compare against .value and by equality only. Enum.__format__ and __str__
    # for mixin enums changed between 3.11 and 3.12, so asserting on f-string
    # output would make this test Python-version dependent.
    assert PriceStatus.OK == "ok"
    assert PriceStatus.NOT_FOUND.value == "not_found"
    assert PriceStatus.SUSPECT.value == "suspect"


def _ok_reading() -> PriceReading:
    return PriceReading(
        timestamp=datetime(2026, 9, 11, 6, 0, 0),
        item="Widget",
        currency="GBP",
        status=PriceStatus.OK,
    )


def test_reading_defaults_leave_every_detection_absent():
    """A failed sweep is exactly this reading: a status and nothing else."""
    reading = _ok_reading()
    assert reading.best_rated is None
    assert reading.cheapest is None
    assert reading.best_value is None


def test_reading_defaults_carry_no_confidence_source_count_or_note():
    reading = _ok_reading()
    assert reading.confidence is None
    assert reading.sources is None
    assert reading.note == ""


def test_a_reading_carries_all_three_detections():
    reading = PriceReading(
        timestamp=datetime(2026, 9, 11, 6, 0, 0),
        item="Widget",
        currency="GBP",
        status=PriceStatus.OK,
        best_rated=Detection(
            price=120.0, site="john-lewis.com", rating="4.6 Trustpilot"
        ),
        cheapest=Detection(price=100.0, site="cheap.example"),
        best_value=Detection(price=110.0, site="argos.co.uk", rating="4.4 Trustpilot"),
    )
    assert reading.best_rated.site == "john-lewis.com"
    assert reading.cheapest.price == 100.0
    assert reading.best_value.rating == "4.4 Trustpilot"


STEP = 0.05


def _stats(mean: float, last: float, count: int = 4) -> PriceStats:
    return PriceStats(mean=mean, last=last, count=count)


def test_a_price_crossing_below_the_mean_enters_the_first_band():
    assert _stats(mean=100.0, last=105.0).new_drop_band(99.0, STEP) == 1


def test_a_price_holding_its_band_is_not_a_new_drop():
    """The next refresh must not re-announce a drop already reported."""
    assert _stats(mean=100.0, last=99.0).new_drop_band(98.0, STEP) == 0


def test_a_price_falling_into_a_deeper_band_is_a_new_drop():
    """A slow slide keeps reporting, one message per step it gives up."""
    assert _stats(mean=100.0, last=99.0).new_drop_band(94.0, STEP) == 2


def test_each_further_step_down_opens_another_band():
    assert _stats(mean=100.0, last=94.0).new_drop_band(89.0, STEP) == 3


def test_a_price_level_with_the_mean_has_not_dropped():
    assert _stats(mean=100.0, last=105.0).new_drop_band(100.0, STEP) == 0


def test_a_price_recovering_to_the_mean_re_arms_the_alert():
    assert _stats(mean=100.0, last=100.0).new_drop_band(90.0, STEP) == 3


def test_two_readings_are_too_little_history_to_alert_on():
    """A mean built from one or two readings tracks jitter, not a baseline."""
    assert _stats(mean=100.0, last=105.0, count=2).new_drop_band(90.0, STEP) == 0


def test_three_readings_are_enough_history_to_alert_on():
    assert _stats(mean=100.0, last=105.0, count=3).new_drop_band(90.0, STEP) == 3


def test_a_wider_step_puts_the_same_fall_in_a_shallower_band():
    stats = _stats(mean=100.0, last=99.0)
    assert stats.new_drop_band(89.0, 0.05) == 3
    assert stats.new_drop_band(89.0, 0.10) == 2


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.argos.co.uk/product/123", "argos.co.uk"),
        ("https://argos.co.uk/product/123", "argos.co.uk"),
        ("http://SHOP.Example/x", "shop.example"),
        ("", ""),
    ],
)
def test_supplier_domain_strips_scheme_and_www(url, expected):
    from price_monitor.models import supplier_domain

    assert supplier_domain(url) == expected


def test_detection_carries_price_site_and_rating():
    detection = Detection(price=99.99, site="argos.co.uk", rating="4.4 Trustpilot")
    assert detection.price == 99.99
    assert detection.site == "argos.co.uk"
    assert detection.rating == "4.4 Trustpilot"


def test_detection_rating_defaults_to_empty_for_an_unrated_retailer():
    assert Detection(price=10.0, site="shop.example").rating == ""
