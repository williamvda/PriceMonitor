"""Turn a parsed model payload into a validated :class:`PriceReading`.

Applies the plausibility ladder that keeps hallucinated or mis-matched prices
out of the recorded history, or flags them where they are merely suspicious.
"""

import math
from datetime import datetime

from price_monitor.app_config import PriceCtrl
from price_monitor.models import Item, PriceReading, PriceStatus


def normalise_price(
    payload: dict, ctrl: PriceCtrl
) -> tuple[float | None, PriceStatus | None, str]:
    """Coerce a reported price to a VAT-inclusive number, or say why not.

    Returns ``(price, None, note)`` on success and ``(None, status, note)`` on
    rejection. Shared by the single-supplier ladder and the market sweep so that
    a market row and a supplier row for the same product stay comparable — if
    only one path added VAT, every comparison the sheet invites would be wrong.
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

    price, failure, extra = normalise_price(payload, ctrl)
    if failure is not None:
        return reading(None, failure, extra)
    if extra:
        note = f"{note}; {extra}" if note else extra

    if last_price is not None and last_price > 0:
        move = abs(price - last_price) / last_price
        if move > ctrl.suspect_threshold:
            return reading(
                price, PriceStatus.SUSPECT, f"moved {move:.0%} from {last_price}"
            )

    return reading(price, PriceStatus.OK)
