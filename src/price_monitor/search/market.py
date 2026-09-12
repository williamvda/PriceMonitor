"""UK-wide market sweep.

Asks a search-grounded model what an item costs across UK retailers generally,
then reports three answers from the candidates that survive validation — the
best-rated retailer, the cheapest, and the best compromise between them — with
a confidence score describing how much the sweep supports them. Two calls,
because grounding and structured output are unreliable together. Nothing raises
out of :meth:`price_market`.
"""

import logging
import re
from datetime import datetime

from llmbridge import LLMClient
from llmbridge.exceptions import LLMError
from py_utils.logger import get_child_logger

from price_monitor.app_config import LLMConfig, PriceCtrl
from price_monitor.models import Item, PriceReading, PriceStatus
from price_monitor.search.confidence import Candidate, score
from price_monitor.search.detections import select, to_detection
from price_monitor.search.gemini_search import grounding_urls, was_truncated
from price_monitor.search.json_extract import extract_json
from price_monitor.search.prompts import market_format_prompt, market_prompt
from price_monitor.search.searcher import FORMAT_PROVIDER, retrying
from price_monitor.search.validation import normalise_price

# Ratings are reported out of 5. A value outside [0, MAX_RATING] is not a
# trustworthy score — e.g. the model reported one out of 10 — so it is
# treated as unknown rather than trusted.
MAX_RATING = 5.0


# The search call is asked to sign off with "END: <n> retailers". Its absence
# says nothing on its own — a model that forgets the line must not cost a
# sweep its prices — but when present it is the only evidence of how many
# retailers the prose named before the second call converted it.
_END_MARKER = re.compile(r"END:\s*(\d+)", re.IGNORECASE)


def _declared_count(report: str) -> int | None:
    """How many retailers the search call says it listed, if it said."""
    matches = _END_MARKER.findall(report)
    return int(matches[-1]) if matches else None


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
        if not config.grounded:
            self.logger.warning(
                "Grounding disabled: prices come from model recall, and "
                "source_url is unverified. The sourcing factor is capped for "
                "every sweep."
            )
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

        truncated = was_truncated(searched.raw_response)
        if truncated:
            self.logger.warning(
                f"Market search for '{item.name}' hit max_tokens — the report "
                f"was cut off, so retailers it had not yet named are missing "
                f"from this sweep. Raise llm_config.max_tokens."
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

        return self._rank(
            payload,
            item,
            timestamp,
            last_price,
            urls,
            truncated,
            _declared_count(searched.text),
        )

    def _rank(
        self,
        payload: dict,
        item: Item,
        timestamp: datetime,
        last_price: float | None,
        urls: list[str],
        truncated: bool = False,
        declared: int | None = None,
    ) -> PriceReading:
        pairs = self._candidates(payload)
        if not pairs:
            note = str(payload.get("note") or "").strip() or "no usable candidates"
            return self._failed(item, timestamp, PriceStatus.NOT_FOUND, note)

        candidates = [candidate for candidate, _ in pairs]
        rated_pick, cheapest_pick, value_pick = select(
            candidates, self._ctrl.rating_premium_pct
        )
        if cheapest_pick is None:
            # select() only returns None across the board when `candidates`
            # is empty, which cannot happen here (`pairs` was just checked
            # above) — but `Candidate | None` doesn't say so, and this module
            # promises nothing raises out of price_market(), so the guarantee
            # is made explicit rather than assumed.
            return self._failed(
                item, timestamp, PriceStatus.NOT_FOUND, "no usable candidates"
            )
        # `is`, not `==`: two candidates can share every field value, and only
        # the object actually chosen carries the right VAT note.
        winner_vat_note = next(n for c, n in pairs if c is cheapest_pick)
        value, reason = score(candidates, cheapest_pick, urls)

        status = PriceStatus.OK
        note = reason
        if truncated:
            # On the sheet, not just in the log: a sweep that lost candidates
            # to the token ceiling looks identical to a thorough one.
            note = f"{note}; report truncated, retailers may be missing"
        if dropped := self._dropped_in_conversion(payload, declared):
            note = f"{note}; {dropped}"
        if winner_vat_note:
            note = f"{note}; {winner_vat_note}"
        if last_price is not None and last_price > 0:
            move = abs(cheapest_pick.price - last_price) / last_price
            if move > self._ctrl.suspect_threshold:
                status = PriceStatus.SUSPECT
                note = f"{note}; moved {move:.0%} from {last_price}"

        return PriceReading(
            timestamp=timestamp,
            item=item.name,
            currency=self._ctrl.currency,
            status=status,
            best_rated=to_detection(rated_pick),
            cheapest=to_detection(cheapest_pick),
            best_value=to_detection(value_pick),
            confidence=value,
            # Survivors, not the number the model named: a retailer that is
            # out of stock or failed validation cannot sell it, and this is
            # the same count corroboration scored.
            sources=len(candidates),
            note=note,
        )

    def _dropped_in_conversion(self, payload: dict, declared: int | None) -> str:
        """Say so when the JSON call kept fewer retailers than the prose named.

        The two calls are the seam nothing else watches: the search call can
        name six retailers and the conversion emit three, leaving valid JSON,
        a healthy status and no trace of the three that vanished. A model that
        omits the count simply disables the check — it must never cost a sweep
        its prices.
        """
        if declared is None:
            return ""
        converted = payload.get("candidates")
        kept = len(converted) if isinstance(converted, list) else 0
        if kept >= declared:
            return ""
        self.logger.warning(
            f"Conversion kept {kept} of the {declared} retailers the search "
            f"reported — the rest were lost between the two calls."
        )
        return f"converted {kept} of {declared} retailers reported"

    def _candidates(self, payload: dict) -> list[tuple[Candidate, str]]:
        """Validated, in-stock candidates, VAT-normalised.

        Each candidate is paired with the VAT-adjustment note
        :func:`normalise_price` returned for it (blank when its price needed
        no adjustment), so the winner's note can be preserved without
        carrying VAT fields on :class:`Candidate` itself.
        """
        candidates: list[tuple[Candidate, str]] = []
        raw_candidates = payload.get("candidates")
        if not isinstance(raw_candidates, list):
            return candidates
        for raw in raw_candidates:
            if not isinstance(raw, dict):
                continue

            price, vat_note = normalise_price(raw, self._ctrl)
            if price is None:
                continue
            if raw.get("in_stock", True) is False:
                continue

            rating = _as_float(raw.get("rating"))
            if rating is not None and not (0.0 <= rating <= MAX_RATING):
                # Ratings are out of 5. A value outside that range (a rating
                # out of 10, or a review count mistaken for a score) is not a
                # trustworthy 5-point score, so it is treated as unknown
                # rather than displayed as one.
                rating = None
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
            candidates.append((candidate, vat_note))
        return candidates

    def _failed(
        self, item: Item, timestamp: datetime, status: PriceStatus, note: str
    ) -> PriceReading:
        return PriceReading(
            timestamp=timestamp,
            item=item.name,
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
