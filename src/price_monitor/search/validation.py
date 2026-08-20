"""Turn a parsed model payload into a validated :class:`PriceReading`.

Applies the plausibility ladder that keeps hallucinated or mis-matched prices
out of the recorded history, or flags them where they are merely suspicious.
"""

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
    """Grade ``payload`` against the plausibility ladder."""
    source_url = str(payload.get("url") or "").strip()
    if not source_url and fallback_urls:
        source_url = fallback_urls[0]
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

    try:
        price = float(payload.get("price"))
    except (TypeError, ValueError):
        return reading(None, PriceStatus.REJECTED, "price was not a number")

    if price <= 0 or price > ctrl.max_plausible_price:
        return reading(None, PriceStatus.REJECTED, f"implausible price {price}")

    if last_price is not None and last_price > 0:
        move = abs(price - last_price) / last_price
        if move > ctrl.suspect_threshold:
            return reading(
                price, PriceStatus.SUSPECT, f"moved {move:.0%} from {last_price}"
            )

    return reading(price, PriceStatus.OK)
