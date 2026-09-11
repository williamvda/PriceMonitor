"""Currency/VAT/plausibility coercion for a reported price.

Exports :func:`normalise_price`, which turns a raw reported price into a
VAT-inclusive number ready to compare and record, or explains why it was
rejected.
"""

import math

from price_monitor.app_config import PriceCtrl
from price_monitor.models import PriceStatus


def normalise_price(
    payload: dict, ctrl: PriceCtrl
) -> tuple[float | None, PriceStatus | None, str]:
    """Coerce a reported price to a VAT-inclusive number, or say why not.

    Returns ``(price, None, note)`` on success and ``(None, status, note)`` on
    rejection.
    """
    currency = str(payload.get("currency") or "").strip().upper()
    if currency != ctrl.currency.strip().upper():
        return None, PriceStatus.WRONG_CURRENCY, f"reported {currency or '?'}"

    if isinstance(payload.get("price"), bool):
        return None, PriceStatus.REJECTED, "price was a boolean"

    try:
        price = float(payload.get("price"))
    except (TypeError, ValueError):
        return None, PriceStatus.REJECTED, "price was not a number"

    if math.isnan(price) or math.isinf(price):
        return None, PriceStatus.REJECTED, f"price was {price}"

    note = ""
    # Recorded history is always VAT-inclusive. Absent vat_included, assume the
    # price already includes VAT — adding tax to an inclusive price would invent
    # a jump that never happened, which is the worse of the two errors.
    if payload.get("vat_included", True) is False:
        ex_vat = price
        price = round(price * (1 + ctrl.vat_rate), 2)
        note = f"ex-VAT {ex_vat:g} +{ctrl.vat_rate:.0%} VAT"

    if price <= 0 or price > ctrl.max_plausible_price:
        return None, PriceStatus.REJECTED, f"implausible price {price}"

    return price, None, note
