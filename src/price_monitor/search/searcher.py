"""Shared retry loop for the search-provider calls in ``market.py``.

Both the search and format calls retry transient server-side failures with
exponential backoff; see :func:`retrying` for the retry mechanism and
:func:`_is_retryable` for what counts as transient.

Exports :func:`retrying` (the retry loop) and :const:`FORMAT_PROVIDER` (the
un-grounded provider name), both consumed by
:class:`~price_monitor.search.market.MarketSearcher`.
"""

import logging
import time
from typing import Callable

from llmbridge.exceptions import LLMConnectionError, LLMError, LLMResponseError
from llmbridge.models import PromptResponse

# Call two must not be grounded: it is the un-grounded regime that makes JSON
# output reliable, which is the whole reason the lookup is split in two.
FORMAT_PROVIDER = "gemini"

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


def retrying(
    call: Callable[[], PromptResponse], label: str, logger: logging.Logger
) -> PromptResponse:
    """Run ``call``, retrying transient failures with exponential backoff.

    The final attempt re-raises rather than swallowing, so the caller maps the
    failure to a status itself.
    """
    delay = _RETRY_BASE_DELAY_S
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return call()
        except Exception as exc:
            if attempt == _RETRY_ATTEMPTS or not _is_retryable(exc):
                raise
            logger.warning(
                f"Transient {label} failure "
                f"(attempt {attempt}/{_RETRY_ATTEMPTS}), "
                f"retrying in {delay:.0f}s: {exc}"
            )
            time.sleep(delay)
            delay *= 2
    raise LLMError(f"{label} call exhausted retries")  # unreachable
