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


def test_an_out_of_range_rating_is_treated_as_unrated(logger):
    """Ratings are out of 5. A model reporting 45.0 (out of 10, or a review
    count mistaken for a score) must not sail past the floor and score a
    full 1.0 on reputation — it is treated as unknown instead."""
    searcher = _searcher(
        logger,
        [
            _candidate(
                price=100.0, rating=45.0, rating_count=900, url="https://a.co.uk/w"
            ),
        ],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.status == PriceStatus.OK
    assert reading.price == 100.0
    assert reading.rating == ""


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


def test_the_winning_candidates_vat_adjustment_is_recorded_in_the_note(logger):
    """The winner's own VAT top-up must be visible in the note, appended
    after the confidence reason — a market row that adjusted for VAT must
    not look identical to one that did not."""
    searcher = _searcher(
        logger,
        [_candidate(price=90.0, vat_included=False, url="https://a.co.uk/w")],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.price == 108.0
    # Appended after the confidence reason, in the same "; "-joined style as
    # the suspect-movement text.
    assert reading.note.endswith("; ex-VAT 90 +20% VAT")


def test_a_discarded_candidates_vat_adjustment_is_not_recorded(logger):
    """Only the winner's note matters; a cheaper VAT-adjusted candidate that
    lost the race leaves no trace of its own adjustment."""
    searcher = _searcher(
        logger,
        [
            _candidate(price=200.0, vat_included=False, url="https://a.co.uk/w"),
            _candidate(price=100.0, vat_included=True, url="https://b.co.uk/w"),
        ],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.price == 100.0
    assert "ex-VAT" not in reading.note


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


def test_a_non_list_candidates_field_is_not_found(logger):
    """A count where a list was expected — shape-collapse an LLM can produce."""
    searcher = _searcher(logger, 1)
    assert searcher.price_market(ITEM, None, NOW).status == PriceStatus.NOT_FOUND


def test_a_non_dict_entry_in_candidates_is_ignored(logger):
    searcher = _searcher(logger, ["not a dict", _candidate(price=100.0)])
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.status == PriceStatus.OK
    assert reading.price == 100.0


def test_a_boolean_rating_is_treated_as_unrated_not_as_one(logger):
    """True as a rating must not parse as 1.0 and trip the floor."""
    searcher = _searcher(
        logger,
        [_candidate(price=100.0, rating=True, rating_count=900)],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.status == PriceStatus.OK
    assert reading.price == 100.0
    assert reading.rating == ""


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


def test_a_sweep_records_how_many_retailers_it_found(logger):
    searcher = _searcher(
        logger,
        [
            _candidate(price=120.0, url="https://a.co.uk/w"),
            _candidate(price=100.0, url="https://b.co.uk/w"),
            _candidate(price=110.0, url="https://c.co.uk/w"),
        ],
    )
    assert searcher.price_market(ITEM, None, NOW).sources == 3


def test_a_single_retailer_sweep_records_one_source(logger):
    """The signal the user acts on: only one retailer could actually sell it."""
    searcher = _searcher(logger, [_candidate(price=100.0)])
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.sources == 1


def test_the_source_count_excludes_candidates_that_were_dropped(logger):
    """Counts retailers that survived validation, not ones the model named.

    Five reported but four unusable means one retailer can actually sell it,
    which is the fact worth surfacing — and the same count corroboration scored.
    """
    searcher = _searcher(
        logger,
        [
            _candidate(price=100.0, url="https://keep.co.uk/w"),
            _candidate(price=10.0, in_stock=False, url="https://a.co.uk/w"),
            _candidate(price=20.0, currency="USD", url="https://b.co.uk/w"),
            _candidate(
                price=30.0, rating=2.1, rating_count=900, url="https://c.co.uk/w"
            ),
            _candidate(price=-5.0, url="https://d.co.uk/w"),
        ],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.sources == 1
    assert reading.price == 100.0


def test_a_failed_sweep_records_no_source_count(logger):
    """`error`/`parse_error` never got far enough to know; blank, not zero."""
    searcher = _searcher(logger, [])
    assert searcher.price_market(ITEM, None, NOW).sources is None
