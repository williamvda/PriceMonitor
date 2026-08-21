"""Turn a parsed model payload into a validated :class:`PriceReading`.

Applies the plausibility ladder that keeps hallucinated or mis-matched prices
out of the recorded history, or flags them where they are merely suspicious.
"""

import math
from datetime import datetime

from price_monitor.app_config import PriceCtrl
from price_monitor.models import Item, PriceReading, PriceStatus


def validate(
    payload: dict,
    item: Item,
    timestamp: datetime,
    ctrl: PriceCtrl,
    last_price: float | None,
    fallback_urls: list[str],
) -> PriceReading:
    """Grade ``payload`` against the plausibility ladder.

    A price flagged ``vat_included: false`` has ``ctrl.vat_rate`` added before
    any plausibility or movement check, so every recorded price — and every
    comparison against ``last_price`` — is VAT-inclusive.
    """
    source_url = str(payload.get("url") or "").strip()
    if not source_url:
        source_url = next(
            (u.strip() for u in fallback_urls if u and str(u).strip()), ""
        )
    note = str(payload.get("note") or "").strip()

    def reading(
        price: float | None, status: PriceStatus, text: str = ""
    ) -> PriceReading:
        return PriceReading(
            timestamp=timestamp,
            item=item.name,
            website=item.website,
            price=price,
            currency=ctrl.currency,
            status=status,
            source_url=source_url,
            note=text or note,
        )

    if not payload.get("found", False):
        return reading(None, PriceStatus.NOT_FOUND)

    currency = str(payload.get("currency") or "").strip().upper()
    if currency != ctrl.currency.strip().upper():
        return reading(None, PriceStatus.WRONG_CURRENCY, f"reported {currency or '?'}")

    if isinstance(payload.get("price"), bool):
        return reading(None, PriceStatus.REJECTED, "price was a boolean")

    try:
        price = float(payload.get("price"))
    except (TypeError, ValueError):
        return reading(None, PriceStatus.REJECTED, "price was not a number")

    if math.isnan(price) or math.isinf(price):
        return reading(None, PriceStatus.REJECTED, f"price was {price}")

    # Recorded history is always VAT-inclusive, so a run that read an ex-VAT
    # figure stays comparable with one that read the inc-VAT figure for the
    # same product. Absent vat_included, assume the price already includes VAT
    # — adding tax to an inclusive price would invent a jump that never
    # happened, which is the worse of the two errors.
    if payload.get("vat_included", True) is False:
        ex_vat = price
        price = round(price * (1 + ctrl.vat_rate), 2)
        vat_note = f"ex-VAT {ex_vat:g} +{ctrl.vat_rate:.0%} VAT"
        note = f"{note}; {vat_note}" if note else vat_note

    if price <= 0 or price > ctrl.max_plausible_price:
        return reading(None, PriceStatus.REJECTED, f"implausible price {price}")

    if last_price is not None and last_price > 0:
        move = abs(price - last_price) / last_price
        if move > ctrl.suspect_threshold:
            return reading(
                price, PriceStatus.SUSPECT, f"moved {move:.0%} from {last_price}"
            )

    return reading(price, PriceStatus.OK)
