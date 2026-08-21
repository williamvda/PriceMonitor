"""Unit tests for price_monitor.models value objects."""

from datetime import datetime

from price_monitor.models import Item, PriceReading, PriceStatus


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
