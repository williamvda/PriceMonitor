"""Unit tests for the retry loop shared by every search call."""

import logging

import pytest
from llmbridge.exceptions import (
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
)
from llmbridge.models import PromptResponse

import price_monitor.search.searcher as searcher_module
from price_monitor.search.searcher import (
    _RETRY_ATTEMPTS,
    FORMAT_PROVIDER,
    SpendCapReached,
    retrying,
)


def _response(text: str = "ok") -> PromptResponse:
    return PromptResponse(
        text=text,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        model="gemini-3.7-flash",
        provider="gemini",
        latency_ms=1.0,
        raw_response={},
    )


class FakeCall:
    """Replays queued responses or raises queued errors, counting attempts."""

    def __init__(self, *results):
        self.results = list(results)
        self.attempts = 0

    def __call__(self) -> PromptResponse:
        self.attempts += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def logger():
    return logging.getLogger("test")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Record backoff delays instead of really sleeping through them."""
    delays: list[float] = []
    monkeypatch.setattr(searcher_module.time, "sleep", delays.append)
    return delays


def test_a_clean_call_is_made_once(logger, no_sleep):
    call = FakeCall(_response("fine"))
    assert retrying(call, "search", logger).text == "fine"
    assert call.attempts == 1
    assert no_sleep == []


def test_a_transient_failure_is_retried_then_succeeds(logger, no_sleep):
    call = FakeCall(LLMResponseError("high demand", status_code=503), _response())
    assert retrying(call, "search", logger).text == "ok"
    assert call.attempts == 2
    assert no_sleep == [2.0]


def test_backoff_doubles_between_attempts(logger, no_sleep):
    call = FakeCall(*[LLMConnectionError("down")] * _RETRY_ATTEMPTS)
    with pytest.raises(LLMError):
        retrying(call, "search", logger)
    assert call.attempts == _RETRY_ATTEMPTS
    assert no_sleep == [2.0, 4.0]


def test_the_final_attempt_re_raises_so_the_caller_maps_the_status(logger):
    call = FakeCall(*[LLMConnectionError("down")] * _RETRY_ATTEMPTS)
    with pytest.raises(LLMConnectionError):
        retrying(call, "search", logger)


def test_a_rate_limit_is_not_retried(logger, no_sleep):
    """A 429 here is a standing project quota, not a burst: retrying it only
    delays the same failure, so the call must give up on the first attempt."""
    call = FakeCall(LLMRateLimitError("quota"))
    with pytest.raises(LLMRateLimitError):
        retrying(call, "search", logger)
    assert call.attempts == 1
    assert no_sleep == []


def test_a_client_error_is_not_retried(logger, no_sleep):
    call = FakeCall(LLMResponseError("bad request", status_code=400))
    with pytest.raises(LLMResponseError):
        retrying(call, "search", logger)
    assert call.attempts == 1
    assert no_sleep == []


def test_a_non_llm_error_is_not_retried_but_still_propagates(logger, no_sleep):
    """llmbridge calls a provider's parse_response() unguarded, so a
    safety-filtered reply raises KeyError straight out of prompt()."""
    call = FakeCall(KeyError("candidates"))
    with pytest.raises(KeyError):
        retrying(call, "search", logger)
    assert call.attempts == 1


def test_call_two_is_never_grounded():
    """The un-grounded regime is what makes JSON output reliable, which is the
    whole reason a lookup is split in two."""
    assert FORMAT_PROVIDER == "gemini"


def test_the_monthly_spend_cap_is_raised_as_its_own_error(logger, no_sleep):
    call = FakeCall(
        LLMRateLimitError(
            "Rate limit exceeded (429): Your project has exceeded its monthly "
            "spending cap."
        )
    )
    with pytest.raises(SpendCapReached):
        retrying(call, "market search", logger)
    assert call.attempts == 1
