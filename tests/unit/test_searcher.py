"""Unit tests for the two-call PriceSearcher orchestration."""

import json
import logging
from datetime import datetime

import pytest
from llmbridge.exceptions import LLMConnectionError, LLMRateLimitError
from llmbridge.models import PromptResponse

from price_monitor.app_config import LLMConfig, PriceCtrl
from price_monitor.models import Item, PriceStatus
from price_monitor.search.searcher import PriceSearcher

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
    search = FakeClient(LLMConnectionError("boom"))
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
