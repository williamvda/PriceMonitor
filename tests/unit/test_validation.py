"""Unit tests for the price validation ladder."""

from datetime import datetime

from price_monitor.app_config import PriceCtrl
from price_monitor.models import Item, PriceStatus
from price_monitor.search.validation import validate

NOW = datetime(2026, 8, 20, 6, 0, 0)
ITEM = Item(name="Widget", website="shop.example")
CTRL = PriceCtrl()


def _payload(**overrides):
    payload = {
        "price": 100.0,
        "currency": "GBP",
        "url": "https://shop.example/widget",
        "in_stock": True,
        "found": True,
        "note": "",
    }
    payload.update(overrides)
    return payload


def _validate(payload, last_price=None, urls=None):
    return validate(payload, ITEM, NOW, CTRL, last_price, urls or [])


def test_clean_reading_is_ok():
    reading = _validate(_payload())
    assert reading.status == PriceStatus.OK
    assert reading.price == 100.0
    assert reading.source_url == "https://shop.example/widget"
    assert reading.item == "Widget"
    assert reading.website == "shop.example"


def test_not_found_blanks_the_price_and_keeps_the_note():
    reading = _validate(_payload(found=False, price=None, note="discontinued"))
    assert reading.status == PriceStatus.NOT_FOUND
    assert reading.price is None
    assert reading.note == "discontinued"


def test_currency_mismatch_blanks_the_price():
    reading = _validate(_payload(currency="USD"))
    assert reading.status == PriceStatus.WRONG_CURRENCY
    assert reading.price is None


def test_currency_comparison_ignores_case_and_padding():
    assert _validate(_payload(currency=" gbp ")).status == PriceStatus.OK


def test_zero_price_is_rejected():
    reading = _validate(_payload(price=0))
    assert reading.status == PriceStatus.REJECTED
    assert reading.price is None


def test_negative_price_is_rejected():
    assert _validate(_payload(price=-5)).status == PriceStatus.REJECTED


def test_absurd_price_is_rejected():
    assert _validate(_payload(price=999_999_999)).status == PriceStatus.REJECTED


def test_non_numeric_price_is_rejected():
    assert _validate(_payload(price="two hundred")).status == PriceStatus.REJECTED


def test_numeric_string_price_is_accepted():
    reading = _validate(_payload(price="279.00"))
    assert reading.status == PriceStatus.OK
    assert reading.price == 279.0


def test_large_move_is_suspect_but_still_recorded():
    reading = _validate(_payload(price=300.0), last_price=100.0)
    assert reading.status == PriceStatus.SUSPECT
    assert reading.price == 300.0


def test_small_move_is_ok():
    assert _validate(_payload(price=110.0), last_price=100.0).status == PriceStatus.OK


def test_first_reading_is_never_suspect():
    assert _validate(_payload(price=100.0), last_price=None).status == PriceStatus.OK


def test_source_url_falls_back_to_grounding_when_blank():
    reading = _validate(_payload(url=""), urls=["https://grounded.example/p"])
    assert reading.source_url == "https://grounded.example/p"


def test_source_url_prefers_the_reported_url():
    reading = _validate(_payload(), urls=["https://grounded.example/p"])
    assert reading.source_url == "https://shop.example/widget"


def test_source_url_blank_when_nothing_available():
    assert _validate(_payload(url=""), urls=[]).source_url == ""
