"""UK-wide market sweep.

Asks a search-grounded model what an item costs across UK retailers generally,
ranks the answers, and records the cheapest one that survives validation with a
confidence score describing how much the sweep supports it. Mirrors
:class:`~price_monitor.search.searcher.PriceSearcher`'s two-call shape for the
same reason: grounding and structured output are unreliable together. Nothing
raises out of :meth:`price_market`.
"""

import logging
from datetime import datetime

from llmbridge import LLMClient
from llmbridge.exceptions import LLMError
from py_utils.logger import get_child_logger

from price_monitor.app_config import LLMConfig, PriceCtrl
from price_monitor.models import MARKET_KEY, Item, PriceReading, PriceStatus
from price_monitor.search.confidence import FLOOR_RATING, Candidate, score
from price_monitor.search.gemini_search import grounding_urls
from price_monitor.search.json_extract import extract_json
from price_monitor.search.prompts import market_format_prompt, market_prompt
from price_monitor.search.searcher import FORMAT_PROVIDER, retrying
from price_monitor.search.validation import normalise_price


class MarketSearcher:
    """Prices a single :class:`Item` across UK retailers rather than at one."""

    def __init__(
        self,
        config: LLMConfig,
        ctrl: PriceCtrl,
        logger: logging.Logger,
        search_client: LLMClient | None = None,
        format_client: LLMClient | None = None,
    ) -> None:
        self.logger = get_child_logger(logger, __class__.__name__)
        self._ctrl = ctrl
        search_provider = config.provider if config.grounded else FORMAT_PROVIDER
        self._search_client = search_client or LLMClient(
            provider=search_provider,
            api_key=config.api_key,
            model=config.model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.timeout,
        )
        self._format_client = format_client or LLMClient(
            provider=FORMAT_PROVIDER,
            api_key=config.api_key,
            model=config.model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.timeout,
        )

    def price_market(
        self, item: Item, last_price: float | None, timestamp: datetime
    ) -> PriceReading:
        """Sweep UK retailers for ``item``, never raising on failure."""
        try:
            searched = retrying(
                lambda: self._search_client.prompt(market_prompt(item)),
                "market search",
                self.logger,
            )
        except (LLMError, KeyError, IndexError, TypeError) as exc:
            # Same unguarded parse_response() exposure as the supplier path: a
            # safety-filtered or truncated reply raises out of prompt() rather
            # than arriving as an LLMError.
            self.logger.warning(f"Market search failed for '{item.name}': {exc}")
            return self._failed(item, timestamp, PriceStatus.ERROR, str(exc))

        if not searched.text or not searched.text.strip():
            self.logger.warning(f"Market search returned no text for '{item.name}'")
            return self._failed(
                item, timestamp, PriceStatus.PARSE_ERROR, "empty search reply"
            )

        urls = grounding_urls(searched.raw_response)

        try:
            formatted = retrying(
                lambda: self._format_client.prompt(
                    market_format_prompt(searched.text, urls)
                ),
                "market format",
                self.logger,
            )
        except (LLMError, KeyError, IndexError, TypeError) as exc:
            self.logger.warning(f"Market format failed for '{item.name}': {exc}")
            return self._failed(item, timestamp, PriceStatus.ERROR, str(exc))

        try:
            payload = extract_json(formatted.text)
        except ValueError as exc:
            self.logger.warning(f"Unparseable market reply for '{item.name}': {exc}")
            return self._failed(item, timestamp, PriceStatus.PARSE_ERROR, str(exc))

        return self._rank(payload, item, timestamp, last_price, urls)

    def _rank(
        self,
        payload: dict,
        item: Item,
        timestamp: datetime,
        last_price: float | None,
        urls: list[str],
    ) -> PriceReading:
        candidates = self._candidates(payload)
        if not candidates:
            note = str(payload.get("note") or "").strip() or "no usable candidates"
            return self._failed(item, timestamp, PriceStatus.NOT_FOUND, note)

        winner = min(candidates, key=lambda c: c.price)
        value, reason = score(candidates, urls)

        status = PriceStatus.OK
        note = reason
        if last_price is not None and last_price > 0:
            move = abs(winner.price - last_price) / last_price
            if move > self._ctrl.suspect_threshold:
                status = PriceStatus.SUSPECT
                note = f"{note}; moved {move:.0%} from {last_price}"

        return PriceReading(
            timestamp=timestamp,
            item=item.name,
            website=MARKET_KEY,
            price=winner.price,
            currency=self._ctrl.currency,
            status=status,
            source_url=winner.url,
            note=note,
            confidence=value,
            rating=winner.rating_label,
        )

    def _candidates(self, payload: dict) -> list[Candidate]:
        """Validated, in-stock, above-floor candidates, VAT-normalised."""
        candidates: list[Candidate] = []
        raw_candidates = payload.get("candidates")
        if not isinstance(raw_candidates, list):
            return candidates
        for raw in raw_candidates:
            if not isinstance(raw, dict):
                continue

            price, failure, _ = normalise_price(raw, self._ctrl)
            if failure is not None or price is None:
                continue
            if raw.get("in_stock", True) is False:
                continue

            rating = _as_float(raw.get("rating"))
            count = _as_int(raw.get("rating_count"))
            candidate = Candidate(
                supplier=str(raw.get("supplier") or "").strip(),
                price=price,
                url=str(raw.get("url") or "").strip(),
                in_stock=True,
                rating=rating,
                rating_count=count,
                rating_source=str(raw.get("rating_source") or "").strip(),
            )
            # The floor needs both halves: a bad score on a handful of reviews
            # is noise. An unrated retailer is never dropped here — plenty of
            # legitimate specialists have no Trustpilot presence, and dropping
            # them would bias every sweep toward the big chains.
            if candidate.rated and candidate.rating < FLOOR_RATING:
                self.logger.info(
                    f"Dropping {candidate.supplier}: rated {candidate.rating} "
                    f"on {candidate.rating_count} reviews"
                )
                continue
            candidates.append(candidate)
        return candidates

    def _failed(
        self, item: Item, timestamp: datetime, status: PriceStatus, note: str
    ) -> PriceReading:
        return PriceReading(
            timestamp=timestamp,
            item=item.name,
            website=MARKET_KEY,
            price=None,
            currency=self._ctrl.currency,
            status=status,
            note=note,
        )


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
