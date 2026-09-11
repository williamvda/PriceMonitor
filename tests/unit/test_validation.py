"""Unit tests for the shared currency/VAT/plausibility coercion."""

from price_monitor.app_config import PriceCtrl
from price_monitor.models import PriceStatus
from price_monitor.search.validation import normalise_price

CTRL = PriceCtrl()


def _normalise(ctrl: PriceCtrl = CTRL, **overrides):
    payload = {"price": 100.0, "currency": "GBP"}
    payload.update(overrides)
    return normalise_price(payload, ctrl)


def test_a_clean_price_is_accepted():
    assert _normalise() == (100.0, None, "")


def test_a_numeric_string_price_is_accepted():
    assert _normalise(price="279.00")[0] == 279.0


def test_a_foreign_currency_is_rejected():
    price, failure, note = _normalise(currency="USD")
    assert price is None
    assert failure == PriceStatus.WRONG_CURRENCY
    assert note == "reported USD"


def test_the_currency_comparison_ignores_case_and_padding():
    assert _normalise(currency=" gbp ")[1] is None


def test_a_boolean_price_is_rejected():
    """True must not parse as 1.0 and reach the sheet as a penny price."""
    assert _normalise(price=True)[1] == PriceStatus.REJECTED
    assert _normalise(price=False)[1] == PriceStatus.REJECTED


def test_a_non_numeric_price_is_rejected():
    assert _normalise(price="cheap")[1] == PriceStatus.REJECTED


def test_a_nan_price_is_rejected():
    assert _normalise(price=float("nan"))[1] == PriceStatus.REJECTED
    assert _normalise(price="nan")[1] == PriceStatus.REJECTED


def test_an_infinite_price_is_rejected():
    assert _normalise(price=float("inf"))[1] == PriceStatus.REJECTED
    assert _normalise(price=float("-inf"))[1] == PriceStatus.REJECTED


def test_a_zero_or_negative_price_is_rejected():
    assert _normalise(price=0)[1] == PriceStatus.REJECTED
    assert _normalise(price=-5)[1] == PriceStatus.REJECTED


def test_an_implausible_price_is_rejected():
    assert _normalise(price=CTRL.max_plausible_price + 1)[1] == PriceStatus.REJECTED


def test_vat_is_added_when_the_price_excludes_it():
    price, failure, note = _normalise(price=100.0, vat_included=False)
    assert price == 120.0
    assert failure is None
    assert note == "ex-VAT 100 +20% VAT"


def test_vat_is_not_added_when_the_price_includes_it():
    assert _normalise(price=359.99, vat_included=True) == (359.99, None, "")


def test_a_missing_vat_flag_leaves_the_price_alone():
    """Absent the flag, assume inclusive: inflating an already-inclusive price
    would fabricate a 20% jump, which is worse than under-reporting."""
    assert _normalise(price=359.99) == (359.99, None, "")


def test_the_vat_rate_is_configurable():
    price, _, note = _normalise(
        PriceCtrl(vat_rate=0.05), price=100.0, vat_included=False
    )
    assert price == 105.0
    assert "+5% VAT" in note


def test_the_vat_adjustment_can_push_a_price_past_the_plausibility_ceiling():
    """The ceiling is checked after the top-up, so an ex-VAT price that only
    just fits must not slip through inflated."""
    ctrl = PriceCtrl(max_plausible_price=350.0)
    assert _normalise(ctrl, price=299.99, vat_included=False)[1] == PriceStatus.REJECTED
