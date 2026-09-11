"""Unit tests for the two-call market sweep orchestration."""

import json
import logging
from datetime import datetime

import pytest
from llmbridge.exceptions import LLMConnectionError
from llmbridge.models import PromptResponse

from price_monitor.app_config import LLMConfig, PriceCtrl
from price_monitor.models import MARKET_KEY, Item, PriceStatus
from price_monitor.search.market import MarketSearcher

NOW = datetime(2026, 9, 11, 6, 0, 0)
ITEM = Item(name="Widget", website="shop.example", market=True)


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


def _candidate(**overrides) -> dict:
    raw = {
        "supplier": "Argos",
        "price": 100.0,
        "currency": "GBP",
        "url": "https://argos.co.uk/widget",
        "in_stock": True,
        "vat_included": True,
        "rating": 4.4,
        "rating_count": 900,
        "rating_source": "trustpilot",
    }
    raw.update(overrides)
    return raw


def _searcher(logger, candidates, note="") -> MarketSearcher:
    payload = json.dumps({"candidates": candidates, "note": note})
    return MarketSearcher(
        LLMConfig(api_key="k"),
        PriceCtrl(),
        logger,
        search_client=FakeClient(_response("a prose report")),
        format_client=FakeClient(_response(payload)),
    )


def test_the_cheapest_in_stock_candidate_wins(logger):
    searcher = _searcher(
        logger,
        [
            _candidate(price=120.0, url="https://a.co.uk/w"),
            _candidate(price=100.0, url="https://b.co.uk/w"),
            _candidate(price=110.0, url="https://c.co.uk/w"),
        ],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.status == PriceStatus.OK
    assert reading.price == 100.0
    assert reading.website == MARKET_KEY
    assert reading.item == "Widget"
    assert reading.source_url == "https://b.co.uk/w"
    assert reading.rating == "4.4 Trustpilot"
    assert reading.confidence is not None


def test_out_of_stock_candidates_are_not_eligible_to_win(logger):
    searcher = _searcher(
        logger,
        [
            _candidate(price=50.0, in_stock=False, url="https://a.co.uk/w"),
            _candidate(price=100.0, url="https://b.co.uk/w"),
        ],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.price == 100.0


def test_a_badly_rated_retailer_is_dropped_by_the_floor(logger):
    searcher = _searcher(
        logger,
        [
            _candidate(
                price=50.0, rating=2.1, rating_count=900, url="https://a.co.uk/w"
            ),
            _candidate(price=100.0, url="https://b.co.uk/w"),
        ],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.price == 100.0


def test_a_bad_rating_on_too_few_reviews_does_not_trigger_the_floor(logger):
    """2.1 from 12 reviews is noise, not evidence — the candidate survives."""
    searcher = _searcher(
        logger,
        [
            _candidate(
                price=50.0, rating=2.1, rating_count=12, url="https://a.co.uk/w"
            ),
            _candidate(price=100.0, url="https://b.co.uk/w"),
        ],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.price == 50.0


def test_an_unrated_retailer_survives_the_floor(logger):
    """Dropping unknowns would bias every sweep toward the big chains."""
    searcher = _searcher(
        logger,
        [
            _candidate(
                price=50.0, rating=None, rating_count=None, url="https://a.co.uk/w"
            ),
        ],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.price == 50.0
    assert reading.rating == ""


def test_a_foreign_currency_candidate_is_dropped(logger):
    searcher = _searcher(
        logger,
        [
            _candidate(price=50.0, currency="USD", url="https://a.co.uk/w"),
            _candidate(price=100.0, url="https://b.co.uk/w"),
        ],
    )
    assert searcher.price_market(ITEM, None, NOW).price == 100.0


def test_an_ex_vat_candidate_is_grossed_up_before_ranking(logger):
    """90 ex-VAT is 108 inc-VAT, so the 100 inc-VAT candidate is cheaper."""
    searcher = _searcher(
        logger,
        [
            _candidate(price=90.0, vat_included=False, url="https://a.co.uk/w"),
            _candidate(price=100.0, url="https://b.co.uk/w"),
        ],
    )
    assert searcher.price_market(ITEM, None, NOW).price == 100.0


def test_no_candidates_is_not_found(logger):
    reading = _searcher(logger, [], note="nobody stocks it").price_market(
        ITEM, None, NOW
    )
    assert reading.status == PriceStatus.NOT_FOUND
    assert reading.price is None
    assert reading.website == MARKET_KEY


def test_all_candidates_out_of_stock_is_not_found(logger):
    searcher = _searcher(logger, [_candidate(in_stock=False)])
    assert searcher.price_market(ITEM, None, NOW).status == PriceStatus.NOT_FOUND


def test_a_big_move_from_the_last_market_price_is_suspect(logger):
    searcher = _searcher(logger, [_candidate(price=100.0)])
    reading = searcher.price_market(ITEM, 400.0, NOW)
    assert reading.status == PriceStatus.SUSPECT
    assert reading.price == 100.0


def test_unparseable_json_is_a_parse_error(logger):
    searcher = MarketSearcher(
        LLMConfig(api_key="k"),
        PriceCtrl(),
        logger,
        search_client=FakeClient(_response("report")),
        format_client=FakeClient(_response("not json at all")),
    )
    assert searcher.price_market(ITEM, None, NOW).status == PriceStatus.PARSE_ERROR


def test_an_empty_search_reply_is_a_parse_error(logger):
    searcher = MarketSearcher(
        LLMConfig(api_key="k"),
        PriceCtrl(),
        logger,
        search_client=FakeClient(_response("   ")),
        format_client=FakeClient(_response("{}")),
    )
    assert searcher.price_market(ITEM, None, NOW).status == PriceStatus.PARSE_ERROR


def test_a_raising_client_is_an_error_not_an_exception(logger):
    searcher = MarketSearcher(
        LLMConfig(api_key="k"),
        PriceCtrl(),
        logger,
        search_client=FakeClient(
            LLMConnectionError("down"),
            LLMConnectionError("down"),
            LLMConnectionError("down"),
        ),
        format_client=FakeClient(_response("{}")),
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.status == PriceStatus.ERROR
    assert reading.website == MARKET_KEY
