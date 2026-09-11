"""Unit tests for price_monitor.models value objects."""

from datetime import datetime

import pytest

from price_monitor.models import Item, PriceReading, PriceStats, PriceStatus


def test_bare_domain_is_not_a_direct_url():
    assert Item(name="Widget", website="amazon.co.uk").is_direct_url is False


def test_scheme_only_is_not_a_direct_url():
    assert Item(name="Widget", website="https://amazon.co.uk").is_direct_url is False


def test_trailing_slash_is_not_a_direct_url():
    assert Item(name="Widget", website="https://amazon.co.uk/").is_direct_url is False


def test_www_prefix_is_not_a_direct_url():
    assert Item(name="Widget", website="www.amazon.co.uk").is_direct_url is False


def test_product_path_is_a_direct_url():
    item = Item(name="Widget", website="https://www.amazon.co.uk/dp/B09XS7JWHH")
    assert item.is_direct_url is True


def test_schemeless_product_path_is_a_direct_url():
    assert Item(name="Widget", website="amazon.co.uk/dp/B09").is_direct_url is True


def test_status_serialises_as_its_string_value():
    # Compare against .value and by equality only. Enum.__format__ and __str__
    # for mixin enums changed between 3.11 and 3.12, so asserting on f-string
    # output would make this test Python-version dependent.
    assert PriceStatus.OK == "ok"
    assert PriceStatus.NOT_FOUND.value == "not_found"
    assert PriceStatus.SUSPECT.value == "suspect"


def test_reading_defaults_leave_source_and_note_blank():
    reading = PriceReading(
        timestamp=datetime(2026, 8, 20, 6, 0, 0),
        item="Widget",
        website="amazon.co.uk",
        price=279.0,
        currency="GBP",
        status=PriceStatus.OK,
    )
    assert reading.source_url == ""
    assert reading.note == ""


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


def test_item_defaults_to_not_market_tracked():
    assert Item(name="Widget", website="shop.example").market is False


def test_item_can_opt_into_market_tracking():
    assert Item(name="Widget", website="shop.example", market=True).market is True


def test_market_key_is_the_sentinel_website():
    from price_monitor.models import MARKET_KEY

    assert MARKET_KEY == "market"


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


def test_reading_defaults_carry_no_source_count():
    """A supplier reading has no retailer count; only a sweep does."""
    reading = PriceReading(
        timestamp=datetime(2026, 9, 11, 6, 0, 0),
        item="Widget",
        website="shop.example",
        price=10.0,
        currency="GBP",
        status=PriceStatus.OK,
    )
    assert reading.sources is None


def test_reading_defaults_carry_no_confidence_or_rating():
    reading = PriceReading(
        timestamp=datetime(2026, 9, 11, 6, 0, 0),
        item="Widget",
        website="shop.example",
        price=10.0,
        currency="GBP",
        status=PriceStatus.OK,
    )
    assert reading.confidence is None
    assert reading.rating == ""
