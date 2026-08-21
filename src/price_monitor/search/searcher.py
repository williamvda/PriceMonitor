"""Two-call price lookup.

Call one asks a search-grounded model for a prose report; call two converts
that report to JSON with grounding switched off, because Gemini's grounding and
structured output are unreliable together. Every failure path returns a
:class:`PriceReading` carrying a status — nothing raises out of :meth:`price`.

Both calls retry transient server-side failures with exponential backoff; see
:func:`_is_retryable` for what counts. Grounding on call one can be switched
off via ``LLMConfig.grounded``.
"""

import logging
import time
from datetime import datetime
from typing import Callable

from llmbridge import LLMClient
from llmbridge.exceptions import LLMConnectionError, LLMError, LLMResponseError
from llmbridge.models import PromptResponse
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

_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_S = 2.0
# 5xx only. A 429 is deliberately excluded: on this API it signals a standing
# project quota (Search grounding is a billed feature), not a burst that clears
# on its own, so retrying it would turn an immediate, accurate error status
# into the same error delayed by the whole backoff schedule.
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})


def _is_retryable(exc: Exception) -> bool:
    """True for failures a second attempt could plausibly survive."""
    if isinstance(exc, LLMConnectionError):
        return True
    return isinstance(exc, LLMResponseError) and exc.status_code in _RETRYABLE_STATUS


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
        search_provider = config.provider if config.grounded else _FORMAT_PROVIDER
        if not config.grounded:
            self.logger.warning(
                "Grounding disabled: prices come from model recall, and "
                "source_url is unverified."
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
            searched = self._retrying(
                lambda: self._search_client.prompt(search_prompt(item)), "search"
            )
        except (LLMError, KeyError, IndexError, TypeError) as exc:
            # llmbridge's LLMClient.prompt() wraps only transport failures
            # (_http_post) as LLMError; it calls the provider's
            # parse_response() unguarded. The stock Gemini parser indexes
            # candidates[0]/parts[0] with no .get() guards, so a
            # safety-filtered response (empty candidates) or a MAX_TOKENS
            # truncation (empty parts) raises IndexError/KeyError straight
            # out of prompt() rather than coming back as an LLMError.
            self.logger.warning(f"Search call failed for '{item.name}': {exc}")
            return self._failed(item, timestamp, PriceStatus.ERROR, str(exc))

        if not searched.text or not searched.text.strip():
            self.logger.warning(f"Search returned no text for '{item.name}'")
            return self._failed(
                item, timestamp, PriceStatus.PARSE_ERROR, "empty search reply"
            )

        urls = grounding_urls(searched.raw_response)

        try:
            formatted = self._retrying(
                lambda: self._format_client.prompt(format_prompt(searched.text, urls)),
                "format",
            )
        except (LLMError, KeyError, IndexError, TypeError) as exc:
            # Same unguarded parse_response() exposure as call one above —
            # the un-grounded stock GeminiProvider is not immune to it.
            self.logger.warning(f"Format call failed for '{item.name}': {exc}")
            return self._failed(item, timestamp, PriceStatus.ERROR, str(exc))

        try:
            payload = extract_json(formatted.text)
        except ValueError as exc:
            self.logger.warning(f"Unparseable reply for '{item.name}': {exc}")
            return self._failed(item, timestamp, PriceStatus.PARSE_ERROR, str(exc))

        return validate(payload, item, timestamp, self._ctrl, last_price, urls)

    def _retrying(
        self, call: Callable[[], PromptResponse], label: str
    ) -> PromptResponse:
        """Run ``call``, retrying transient failures with exponential backoff.

        The final attempt re-raises rather than swallowing, so :meth:`price`
        maps the failure to a status exactly as it did before.
        """
        delay = _RETRY_BASE_DELAY_S
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                return call()
            except Exception as exc:
                if attempt == _RETRY_ATTEMPTS or not _is_retryable(exc):
                    raise
                self.logger.warning(
                    f"Transient {label} failure "
                    f"(attempt {attempt}/{_RETRY_ATTEMPTS}), "
                    f"retrying in {delay:.0f}s: {exc}"
                )
                time.sleep(delay)
                delay *= 2
        raise LLMError(f"{label} call exhausted retries")  # unreachable

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
