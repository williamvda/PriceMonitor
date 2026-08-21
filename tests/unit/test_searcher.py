"""Unit tests for the two-call PriceSearcher orchestration."""

import json
import logging
from datetime import datetime

import pytest
from llmbridge.exceptions import (
    LLMConnectionError,
    LLMRateLimitError,
    LLMResponseError,
)
from llmbridge.models import PromptResponse

import price_monitor.search.searcher as searcher_module
from price_monitor.app_config import LLMConfig, PriceCtrl
from price_monitor.models import Item, PriceStatus
from price_monitor.search.searcher import _RETRY_ATTEMPTS, PriceSearcher

NOW = datetime(2026, 8, 20, 6, 0, 0)
ITEM = Item(name="Widget", website="shop.example")


def _response(text: str, raw: dict | None = None) -> PromptResponse:
    return PromptResponse(
        text=text,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        model="gemini-3.7-flash",
        provider="gemini",
        latency_ms=1.0,
        raw_response=raw or {},
    )


class FakeClient:
    """Records prompts and replays queued responses or raises queued errors."""

    def __init__(self, *results):
        self.results = list(results)
        self.prompts: list[str] = []

    def prompt(self, prompt_text: str, **kwargs) -> PromptResponse:
        self.prompts.append(prompt_text)
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


def _searcher(search_client, format_client, logger, ctrl=None):
    return PriceSearcher(
        config=LLMConfig(api_key="k"),
        ctrl=ctrl or PriceCtrl(),
        logger=logger,
        search_client=search_client,
        format_client=format_client,
    )


_GOOD_JSON = json.dumps(
    {
        "price": 279.0,
        "currency": "GBP",
        "url": "https://shop.example/w",
        "in_stock": True,
        "found": True,
        "note": "",
    }
)


def test_happy_path_returns_an_ok_reading(logger):
    search = FakeClient(_response("It costs 279 GBP."))
    fmt = FakeClient(_response(_GOOD_JSON))
    reading = _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert reading.status == PriceStatus.OK
    assert reading.price == 279.0
    assert reading.timestamp == NOW


def test_call_one_prose_is_passed_into_call_two(logger):
    search = FakeClient(_response("It costs 279 GBP at shop.example."))
    fmt = FakeClient(_response(_GOOD_JSON))
    _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert "It costs 279 GBP at shop.example." in fmt.prompts[0]


def test_grounding_urls_reach_call_two(logger):
    raw = {
        "candidates": [
            {
                "groundingMetadata": {
                    "groundingChunks": [{"web": {"uri": "https://g.example/p"}}]
                }
            }
        ]
    }
    search = FakeClient(_response("It costs 279 GBP.", raw))
    fmt = FakeClient(_response(_GOOD_JSON))
    _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert "https://g.example/p" in fmt.prompts[0]


def test_call_one_failure_short_circuits_call_two(logger):
    # A connection error is retryable, so every attempt must fail before
    # price() gives up — otherwise this asserts nothing about call two.
    search = FakeClient(*[LLMConnectionError("boom")] * _RETRY_ATTEMPTS)
    fmt = FakeClient(_response(_GOOD_JSON))
    reading = _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert reading.status == PriceStatus.ERROR
    assert reading.price is None
    assert fmt.prompts == []


def test_rate_limit_on_call_two_yields_error(logger):
    search = FakeClient(_response("It costs 279 GBP."))
    fmt = FakeClient(LLMRateLimitError("slow down"))
    reading = _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert reading.status == PriceStatus.ERROR


def test_non_llm_error_from_call_one_is_caught_not_raised(logger):
    # llmbridge only wraps transport failures as LLMError; a provider's
    # parse_response() can raise a bare KeyError/IndexError straight out of
    # prompt(). price() must catch that too, not just LLMError.
    search = FakeClient(KeyError("candidates"))
    fmt = FakeClient(_response(_GOOD_JSON))
    try:
        reading = _searcher(search, fmt, logger).price(ITEM, None, NOW)
    except Exception as exc:
        pytest.fail(f"price() raised {exc!r} instead of returning a PriceReading")
    assert reading.status == PriceStatus.ERROR
    assert fmt.prompts == []


def test_non_llm_error_from_call_two_is_caught_not_raised(logger):
    search = FakeClient(_response("It costs 279 GBP."))
    fmt = FakeClient(KeyError("candidates"))
    try:
        reading = _searcher(search, fmt, logger).price(ITEM, None, NOW)
    except Exception as exc:
        pytest.fail(f"price() raised {exc!r} instead of returning a PriceReading")
    assert reading.status == PriceStatus.ERROR


def test_empty_call_one_reply_is_a_parse_error(logger):
    search = FakeClient(_response("   "))
    fmt = FakeClient(_response(_GOOD_JSON))
    reading = _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert reading.status == PriceStatus.PARSE_ERROR
    assert fmt.prompts == []


def test_unparseable_call_two_reply_is_a_parse_error(logger):
    search = FakeClient(_response("It costs 279 GBP."))
    fmt = FakeClient(_response("I am afraid I cannot do that."))
    reading = _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert reading.status == PriceStatus.PARSE_ERROR


def test_last_price_drives_the_suspect_check(logger):
    search = FakeClient(_response("It costs 279 GBP."))
    fmt = FakeClient(_response(_GOOD_JSON))
    reading = _searcher(search, fmt, logger).price(ITEM, 50.0, NOW)
    assert reading.status == PriceStatus.SUSPECT


def test_direct_url_item_uses_the_page_prompt(logger):
    item = Item(name="Widget", website="https://shop.example/dp/1")
    search = FakeClient(_response("It costs 279 GBP."))
    fmt = FakeClient(_response(_GOOD_JSON))
    _searcher(search, fmt, logger).price(item, None, NOW)
    assert "Read that page" in search.prompts[0]


def test_transient_failure_is_retried_then_succeeds(logger, no_sleep):
    search = FakeClient(
        LLMResponseError("high demand", status_code=503),
        _response("It costs 279 GBP."),
    )
    fmt = FakeClient(_response(_GOOD_JSON))
    reading = _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert reading.status == PriceStatus.OK
    assert reading.price == 279.0
    assert len(search.prompts) == 2
    assert no_sleep == [2.0]


def test_backoff_doubles_between_attempts(logger, no_sleep):
    search = FakeClient(
        *[LLMResponseError("high demand", status_code=503)] * _RETRY_ATTEMPTS
    )
    fmt = FakeClient(_response(_GOOD_JSON))
    reading = _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert reading.status == PriceStatus.ERROR
    assert len(search.prompts) == _RETRY_ATTEMPTS
    assert no_sleep == [2.0, 4.0]


def test_rate_limit_is_not_retried(logger, no_sleep):
    """A 429 here is a standing project quota, not a burst: retrying it only
    delays the same failure, so price() must give up on the first attempt."""
    search = FakeClient(LLMRateLimitError("quota"))
    fmt = FakeClient(_response(_GOOD_JSON))
    reading = _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert reading.status == PriceStatus.ERROR
    assert len(search.prompts) == 1
    assert no_sleep == []


def test_client_error_is_not_retried(logger, no_sleep):
    search = FakeClient(LLMResponseError("bad request", status_code=400))
    fmt = FakeClient(_response(_GOOD_JSON))
    reading = _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert reading.status == PriceStatus.ERROR
    assert len(search.prompts) == 1
    assert no_sleep == []


def test_call_two_is_retried_as_well(logger, no_sleep):
    search = FakeClient(_response("It costs 279 GBP."))
    fmt = FakeClient(
        LLMResponseError("high demand", status_code=503),
        _response(_GOOD_JSON),
    )
    reading = _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert reading.status == PriceStatus.OK
    assert len(fmt.prompts) == 2


class _RecordingClient:
    """Captures the provider each LLMClient would have been built with."""

    def __init__(self, **kwargs):
        _RecordingClient.providers.append(kwargs["provider"])

    providers: list[str] = []


@pytest.mark.parametrize(
    "grounded, expected_search_provider",
    [(True, "gemini_search"), (False, "gemini")],
)
def test_grounded_flag_picks_the_call_one_provider(
    logger, monkeypatch, grounded, expected_search_provider
):
    _RecordingClient.providers = []
    monkeypatch.setattr(searcher_module, "LLMClient", _RecordingClient)
    PriceSearcher(
        config=LLMConfig(api_key="k", grounded=grounded),
        ctrl=PriceCtrl(),
        logger=logger,
    )
    # Call two is always un-grounded, whatever call one does.
    assert _RecordingClient.providers == [expected_search_provider, "gemini"]
