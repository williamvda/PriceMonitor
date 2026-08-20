"""Two-call price lookup.

Call one asks a search-grounded model for a prose report; call two converts
that report to JSON with grounding switched off, because Gemini's grounding and
structured output are unreliable together. Every failure path returns a
:class:`PriceReading` carrying a status — nothing raises out of :meth:`price`.
"""

import logging
from datetime import datetime

from llmbridge import LLMClient
from llmbridge.exceptions import LLMError
from py_utils.logger import get_child_logger

from price_monitor.app_config import LLMConfig, PriceCtrl
from price_monitor.models import Item, PriceReading, PriceStatus

# Importing this module registers the "gemini_search" provider with
# llmbridge's ProviderRegistry as a side effect — do not remove it even
# though only grounding_urls is used by name below.
from price_monitor.search.gemini_search import grounding_urls
from price_monitor.search.json_extract import extract_json
from price_monitor.search.prompts import format_prompt, search_prompt
from price_monitor.search.validation import validate

# Call two must not be grounded: it is the un-grounded regime that makes JSON
# output reliable, which is the whole reason the lookup is split in two.
_FORMAT_PROVIDER = "gemini"


class PriceSearcher:
    """Prices a single :class:`Item` using a grounded search then a formatter."""

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
        self._search_client = search_client or LLMClient(
            provider=config.provider,
            api_key=config.api_key,
            model=config.model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.timeout,
        )
        self._format_client = format_client or LLMClient(
            provider=_FORMAT_PROVIDER,
            api_key=config.api_key,
            model=config.model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.timeout,
        )

    def price(
        self, item: Item, last_price: float | None, timestamp: datetime
    ) -> PriceReading:
        """Look up ``item``'s current price, never raising on failure."""
        try:
            searched = self._search_client.prompt(search_prompt(item))
        except LLMError as exc:
            self.logger.warning(f"Search call failed for '{item.name}': {exc}")
            return self._failed(item, timestamp, PriceStatus.ERROR, str(exc))

        if not searched.text or not searched.text.strip():
            self.logger.warning(f"Search returned no text for '{item.name}'")
            return self._failed(
                item, timestamp, PriceStatus.PARSE_ERROR, "empty search reply"
            )

        urls = grounding_urls(searched.raw_response)

        try:
            formatted = self._format_client.prompt(format_prompt(searched.text, urls))
        except LLMError as exc:
            self.logger.warning(f"Format call failed for '{item.name}': {exc}")
            return self._failed(item, timestamp, PriceStatus.ERROR, str(exc))

        try:
            payload = extract_json(formatted.text)
        except ValueError as exc:
            self.logger.warning(f"Unparseable reply for '{item.name}': {exc}")
            return self._failed(item, timestamp, PriceStatus.PARSE_ERROR, str(exc))

        return validate(payload, item, timestamp, self._ctrl, last_price, urls)

    def _failed(
        self, item: Item, timestamp: datetime, status: PriceStatus, note: str
    ) -> PriceReading:
        return PriceReading(
            timestamp=timestamp,
            item=item.name,
            website=item.website,
            price=None,
            currency=self._ctrl.currency,
            status=status,
            note=note,
        )
