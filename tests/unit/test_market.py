"""Unit tests for the two-call market sweep orchestration."""

import json
import logging
import os
from datetime import datetime, timedelta

import pytest
from llmbridge.exceptions import LLMConnectionError
from llmbridge.models import PromptResponse

import price_monitor.search.market as market_module
import price_monitor.search.searcher as searcher_module
from price_monitor.app_config import LLMConfig, PriceCtrl
from price_monitor.models import Item, PriceStatus, Rating
from price_monitor.search.market import MarketSearcher, _declared_count

NOW = datetime(2026, 9, 11, 6, 0, 0)
ITEM = Item(name="Widget")


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
    """Skip the retry backoff: three attempts would add six real seconds."""
    monkeypatch.setattr(searcher_module.time, "sleep", lambda _seconds: None)


def _candidate(**overrides) -> dict:
    raw = {
        "supplier": "Argos",
        "price": 100.0,
        "currency": "GBP",
        "url": "https://argos.co.uk/widget",
        "in_stock": True,
        "vat_included": True,
    }
    raw.update(overrides)
    return raw


def _assert_no_detections(reading) -> None:
    """Every failure path leaves all three answers blank, not partly filled."""
    assert reading.best_rated is None
    assert reading.cheapest is None
    assert reading.best_value is None


def _searcher(logger, candidates, note="", report_dir=None) -> MarketSearcher:
    payload = json.dumps({"candidates": candidates, "note": note})
    return MarketSearcher(
        LLMConfig(api_key="k"),
        PriceCtrl(),
        logger,
        search_client=FakeClient(_response("a prose report")),
        format_client=FakeClient(_response(payload)),
        report_dir=report_dir,
    )


def test_a_sweep_reports_all_three_detections(logger):
    """Equally-rated retailers make the cheapest the answer to all three."""
    searcher = _searcher(
        logger,
        [
            _candidate(price=120.0, url="https://a.co.uk/w"),
            _candidate(price=100.0, url="https://b.co.uk/w"),
            _candidate(price=110.0, url="https://c.co.uk/w"),
        ],
    )
    same_rating = Rating(4.4, 900, "trustpilot")
    reading = searcher.price_market(
        ITEM,
        None,
        NOW,
        ratings={
            "a.co.uk": same_rating,
            "b.co.uk": same_rating,
            "c.co.uk": same_rating,
        },
    )
    assert reading.status == PriceStatus.OK
    assert reading.item == "Widget"
    assert reading.cheapest.price == 100.0
    assert reading.cheapest.site == "b.co.uk"
    assert reading.cheapest.rating == "4.4 Trustpilot"
    assert reading.best_rated.price == 100.0
    assert reading.best_value.price == 100.0
    assert reading.confidence is not None


def test_the_three_detections_can_name_three_different_retailers(logger):
    """The point of the sweep: the best-rated, the cheapest and the sensible
    compromise are usually not the same shop."""
    searcher = _searcher(
        logger,
        [
            _candidate(price=80.0, url="https://cheap.co.uk/w"),
            _candidate(price=100.0, url="https://base.co.uk/w"),
            _candidate(price=102.0, url="https://mid.co.uk/w"),
            _candidate(price=300.0, url="https://posh.co.uk/w"),
        ],
    )
    reading = searcher.price_market(
        ITEM,
        None,
        NOW,
        ratings={
            "base.co.uk": Rating(4.0, 900, "trustpilot"),
            "mid.co.uk": Rating(4.5, 900, "trustpilot"),
            "posh.co.uk": Rating(4.9, 900, "trustpilot"),
        },
    )
    assert reading.cheapest.site == "cheap.co.uk"
    assert reading.cheapest.rating == ""
    assert reading.best_rated.site == "posh.co.uk"
    # Half a rating point above the 100.00 baseline buys a 2.50 budget: 4.5
    # costs 2.00 and qualifies, while 4.9 costs 200.00 and does not.
    assert reading.best_value.site == "mid.co.uk"


def test_a_sweep_with_no_rated_candidate_has_no_rated_or_value_detection(logger):
    searcher = _searcher(logger, [_candidate(price=100.0)])
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.cheapest.price == 100.0
    assert reading.best_rated is None
    assert reading.best_value is None


def test_out_of_stock_candidates_are_not_eligible_to_win(logger):
    searcher = _searcher(
        logger,
        [
            _candidate(price=50.0, in_stock=False, url="https://a.co.uk/w"),
            _candidate(price=100.0, url="https://b.co.uk/w"),
        ],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.cheapest.price == 100.0


def test_a_badly_rated_retailer_is_no_longer_dropped(logger):
    """The floor is gone: a 1.4-rated retailer competes on price like anyone.

    Its rating travels to the sheet so the reader can judge it.
    """
    searcher = _searcher(
        logger,
        [
            _candidate(price=50.0, url="https://a.co.uk/w"),
            _candidate(price=100.0, url="https://b.co.uk/w"),
        ],
    )
    reading = searcher.price_market(
        ITEM, None, NOW, ratings={"a.co.uk": Rating(1.4, 33904, "trustpilot")}
    )
    assert reading.cheapest.price == 50.0
    assert reading.cheapest.rating == "1.4 Trustpilot"
    assert reading.sources == 2


def test_an_unrated_retailer_still_competes(logger):
    """Dropping unknowns would bias every sweep toward the big chains."""
    searcher = _searcher(
        logger,
        [_candidate(price=50.0, url="https://a.co.uk/w")],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.cheapest.price == 50.0
    assert reading.cheapest.rating == ""


def test_a_foreign_currency_candidate_is_dropped(logger):
    searcher = _searcher(
        logger,
        [
            _candidate(price=50.0, currency="USD", url="https://a.co.uk/w"),
            _candidate(price=100.0, url="https://b.co.uk/w"),
        ],
    )
    assert searcher.price_market(ITEM, None, NOW).cheapest.price == 100.0


def test_an_ex_vat_candidate_is_grossed_up_before_ranking(logger):
    """90 ex-VAT is 108 inc-VAT, so the 100 inc-VAT candidate is cheaper."""
    searcher = _searcher(
        logger,
        [
            _candidate(price=90.0, vat_included=False, url="https://a.co.uk/w"),
            _candidate(price=100.0, url="https://b.co.uk/w"),
        ],
    )
    assert searcher.price_market(ITEM, None, NOW).cheapest.price == 100.0


def test_the_winning_candidates_vat_adjustment_is_recorded_in_the_note(logger):
    """The winner's own VAT top-up must be visible in the note, appended
    after the confidence reason — a market row that adjusted for VAT must
    not look identical to one that did not."""
    searcher = _searcher(
        logger,
        [_candidate(price=90.0, vat_included=False, url="https://a.co.uk/w")],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.cheapest.price == 108.0
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
    assert reading.cheapest.price == 100.0
    assert "ex-VAT" not in reading.note


def test_no_candidates_is_not_found(logger):
    reading = _searcher(logger, [], note="nobody stocks it").price_market(
        ITEM, None, NOW
    )
    assert reading.status == PriceStatus.NOT_FOUND
    _assert_no_detections(reading)


def test_all_candidates_out_of_stock_is_not_found(logger):
    searcher = _searcher(logger, [_candidate(in_stock=False)])
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.status == PriceStatus.NOT_FOUND
    _assert_no_detections(reading)


def test_a_non_list_candidates_field_is_not_found(logger):
    """A count where a list was expected — shape-collapse an LLM can produce."""
    searcher = _searcher(logger, 1)
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.status == PriceStatus.NOT_FOUND
    _assert_no_detections(reading)


def test_a_non_dict_entry_in_candidates_is_ignored(logger):
    searcher = _searcher(logger, ["not a dict", _candidate(price=100.0)])
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.status == PriceStatus.OK
    assert reading.cheapest.price == 100.0


def test_a_big_move_from_the_last_cheapest_price_is_suspect(logger):
    """The movement check runs against the cheapest series, the only one with
    a mean — and the reading is still recorded, just flagged."""
    searcher = _searcher(logger, [_candidate(price=100.0)])
    reading = searcher.price_market(ITEM, 400.0, NOW)
    assert reading.status == PriceStatus.SUSPECT
    assert reading.cheapest.price == 100.0


def test_unparseable_json_is_a_parse_error(logger):
    searcher = MarketSearcher(
        LLMConfig(api_key="k"),
        PriceCtrl(),
        logger,
        search_client=FakeClient(_response("report")),
        format_client=FakeClient(_response("not json at all")),
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.status == PriceStatus.PARSE_ERROR
    _assert_no_detections(reading)


def test_an_empty_search_reply_is_a_parse_error(logger):
    searcher = MarketSearcher(
        LLMConfig(api_key="k"),
        PriceCtrl(),
        logger,
        search_client=FakeClient(_response("   ")),
        format_client=FakeClient(_response("{}")),
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.status == PriceStatus.PARSE_ERROR
    _assert_no_detections(reading)


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
    _assert_no_detections(reading)


def test_a_raising_format_client_is_an_error_with_no_detections(logger):
    searcher = MarketSearcher(
        LLMConfig(api_key="k"),
        PriceCtrl(),
        logger,
        search_client=FakeClient(_response("a prose report")),
        format_client=FakeClient(
            LLMConnectionError("down"),
            LLMConnectionError("down"),
            LLMConnectionError("down"),
        ),
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.status == PriceStatus.ERROR
    _assert_no_detections(reading)


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

    Four reported but three unusable means one retailer can actually sell it,
    which is the fact worth surfacing — and the same count corroboration scored.
    """
    searcher = _searcher(
        logger,
        [
            _candidate(price=100.0, url="https://keep.co.uk/w"),
            _candidate(price=10.0, in_stock=False, url="https://a.co.uk/w"),
            _candidate(price=20.0, currency="USD", url="https://b.co.uk/w"),
            _candidate(price=-5.0, url="https://d.co.uk/w"),
        ],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.sources == 1
    assert reading.cheapest.price == 100.0


def test_a_not_found_sweep_records_no_source_count(logger):
    """No usable candidates means `not_found`, which never got far enough to
    know a source count; blank, not zero."""
    searcher = _searcher(logger, [])
    assert searcher.price_market(ITEM, None, NOW).sources is None


class _RecordingClient:
    """Captures the provider each LLMClient would have been built with."""

    providers: list[str] = []

    def __init__(self, **kwargs):
        _RecordingClient.providers.append(kwargs["provider"])


@pytest.mark.parametrize(
    "grounded, expected_search_provider",
    [(True, "gemini_search"), (False, "gemini")],
)
def test_grounded_flag_picks_the_call_one_provider(
    logger, monkeypatch, grounded, expected_search_provider
):
    _RecordingClient.providers = []
    monkeypatch.setattr(market_module, "LLMClient", _RecordingClient)
    MarketSearcher(LLMConfig(api_key="k", grounded=grounded), PriceCtrl(), logger)
    # Call two is always un-grounded, whatever call one does.
    assert _RecordingClient.providers == [expected_search_provider, "gemini"]


def test_the_declared_count_is_read_from_the_sign_off():
    assert _declared_count("... report ...\n\nEND: 5 retailers") == 5
    assert _declared_count("END:4 retailers") == 4
    assert _declared_count("end: 3 retailers") == 3


def test_a_report_without_a_sign_off_declares_nothing():
    """A model that forgets the line disables the check; it must not break it."""
    assert _declared_count("a report with no marker") is None
    assert _declared_count("") is None


def test_the_last_sign_off_wins_if_the_model_repeats_itself():
    assert _declared_count("END: 2 retailers ... more ... END: 6 retailers") == 6


def test_retailers_lost_between_the_two_calls_are_recorded(logger):
    """The search prose named four; the conversion emitted two. Valid JSON, a
    healthy status, and nothing else would ever mention the missing two."""
    payload = json.dumps(
        {"candidates": [_candidate(price=100.0), _candidate(price=120.0)], "note": ""}
    )
    searcher = MarketSearcher(
        LLMConfig(api_key="k"),
        PriceCtrl(),
        logger,
        search_client=FakeClient(_response("a report\n\nEND: 4 retailers")),
        format_client=FakeClient(_response(payload)),
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.status == PriceStatus.OK
    assert "converted 2 of 4 retailers reported" in reading.note


def test_a_faithful_conversion_adds_no_note(logger):
    payload = json.dumps(
        {"candidates": [_candidate(price=100.0), _candidate(price=120.0)], "note": ""}
    )
    searcher = MarketSearcher(
        LLMConfig(api_key="k"),
        PriceCtrl(),
        logger,
        search_client=FakeClient(_response("a report\n\nEND: 2 retailers")),
        format_client=FakeClient(_response(payload)),
    )
    assert "retailers reported" not in searcher.price_market(ITEM, None, NOW).note


def test_a_missing_sign_off_leaves_the_sweep_alone(logger):
    """Non-compliance disables the check rather than costing the prices."""
    searcher = _searcher(logger, [_candidate(price=100.0)])
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.status == PriceStatus.OK
    assert reading.cheapest.price == 100.0
    assert "retailers reported" not in reading.note


def test_a_blocked_retailer_never_wins(logger):
    searcher = _searcher(
        logger,
        [
            _candidate(price=50.0, url="https://www.amazon.co.uk/dp/X"),
            _candidate(price=100.0, url="https://argos.co.uk/w"),
        ],
    )
    reading = searcher.price_market(ITEM, None, NOW, blocked={"amazon.co.uk"})
    assert reading.cheapest.site == "argos.co.uk"


def test_a_blocked_retailer_does_not_count_toward_sources(logger):
    """It is not a retailer you would buy from, so it is not corroboration."""
    searcher = _searcher(
        logger,
        [
            _candidate(price=50.0, url="https://amazon.co.uk/dp/X"),
            _candidate(price=100.0, url="https://argos.co.uk/w"),
        ],
    )
    assert searcher.price_market(ITEM, None, NOW, blocked={"amazon.co.uk"}).sources == 1


def test_a_bare_brand_name_blocks_the_whole_domain(logger):
    searcher = _searcher(logger, [_candidate(price=50.0, url="https://amazon.co.uk/x")])
    reading = searcher.price_market(ITEM, None, NOW, blocked={"amazon"})
    assert reading.status == PriceStatus.NOT_FOUND


def test_nothing_is_blocked_by_default(logger):
    searcher = _searcher(logger, [_candidate(price=50.0, url="https://amazon.co.uk/x")])
    assert searcher.price_market(ITEM, None, NOW).cheapest.price == 50.0


def test_a_disappointing_sweep_keeps_its_report(logger, tmp_path):
    """Diagnosing a thin result should not cost another billed search."""
    searcher = _searcher(logger, [_candidate(price=100.0)], report_dir=tmp_path)
    searcher.price_market(ITEM, None, NOW)
    kept = list(tmp_path.glob("*.txt"))
    assert len(kept) == 1
    assert "a prose report" in kept[0].read_text()


def test_a_confident_sweep_keeps_nothing(logger, tmp_path):
    """Retention is for the results you would otherwise have to re-run."""
    searcher = _searcher(
        logger,
        [_candidate(price=100.0, url=f"https://a{n}.co.uk/w") for n in range(4)],
        report_dir=tmp_path,
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.confidence is not None and reading.confidence >= 0.5
    assert list(tmp_path.glob("*.txt")) == []


def test_a_failed_sweep_keeps_its_report(logger, tmp_path):
    searcher = _searcher(logger, [], report_dir=tmp_path)
    assert searcher.price_market(ITEM, None, NOW).status == PriceStatus.NOT_FOUND
    assert len(list(tmp_path.glob("*.txt"))) == 1


def test_reports_older_than_the_keep_window_are_pruned(logger, tmp_path):
    """A long-running process must not fill the disk."""
    stale = tmp_path / "old-report.txt"
    stale.write_text("ancient")
    old = (datetime.now() - timedelta(days=90)).timestamp()
    os.utime(stale, (old, old))

    searcher = _searcher(logger, [_candidate(price=100.0)], report_dir=tmp_path)
    searcher.price_market(ITEM, None, NOW)
    assert not stale.exists()


def test_retention_is_off_when_no_directory_is_configured(logger):
    """Nothing is written, and nothing raises."""
    searcher = _searcher(logger, [_candidate(price=100.0)])
    assert searcher.price_market(ITEM, None, NOW).status == PriceStatus.OK


def test_a_candidates_rating_comes_from_the_cache_not_the_reply(logger):
    """The model no longer reports ratings; a stale-but-stable cached value is
    better than one that changes source between runs."""
    searcher = _searcher(logger, [_candidate(price=100.0, url="https://argos.co.uk/w")])
    reading = searcher.price_market(
        ITEM, None, NOW, ratings={"argos.co.uk": Rating(4.4, 900, "trustpilot")}
    )
    assert reading.cheapest.rating == "4.4 Trustpilot"


def test_an_uncached_retailer_is_unrated_but_still_competes(logger):
    searcher = _searcher(logger, [_candidate(price=100.0, url="https://new.co.uk/w")])
    reading = searcher.price_market(ITEM, None, NOW, ratings={})
    assert reading.cheapest.price == 100.0
    assert reading.cheapest.rating == ""
    assert reading.best_rated is None


def test_a_cached_rating_on_too_few_reviews_is_still_untrusted(logger):
    """MIN_RATING_COUNT governs whether a rating is trusted, which is a
    separate question from whether it is known."""
    searcher = _searcher(logger, [_candidate(price=100.0, url="https://argos.co.uk/w")])
    reading = searcher.price_market(
        ITEM, None, NOW, ratings={"argos.co.uk": Rating(4.9, 12, "trustpilot")}
    )
    assert reading.cheapest.rating == ""


def test_the_sweep_reports_the_sites_it_kept(logger):
    """Only winners reach the history, so the losers must be registered from
    here or they could never be rated."""
    searcher = _searcher(
        logger,
        [
            _candidate(price=100.0, url="https://argos.co.uk/w"),
            _candidate(price=120.0, url="https://currys.co.uk/w"),
        ],
    )
    searcher.price_market(ITEM, None, NOW)
    assert searcher.seen_sites == {"argos.co.uk", "currys.co.uk"}


def test_a_failed_sweep_reports_no_sites(logger):
    """The controller shares one MarketSearcher across every item, so a
    failed sweep must not leave a previous sweep's sites lingering for a
    retailer that no longer appears to be wrongly re-registered."""
    payload = json.dumps(
        {
            "candidates": [_candidate(price=100.0, url="https://argos.co.uk/w")],
            "note": "",
        }
    )
    searcher = MarketSearcher(
        LLMConfig(api_key="k"),
        PriceCtrl(),
        logger,
        search_client=FakeClient(_response("a prose report"), _response("   ")),
        format_client=FakeClient(_response(payload)),
    )
    searcher.price_market(ITEM, None, NOW)
    assert searcher.seen_sites == {"argos.co.uk"}

    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.status == PriceStatus.PARSE_ERROR
    assert searcher.seen_sites == set()


def test_seen_sites_never_records_an_unparseable_domain(logger):
    """A URL that will not parse must not pollute seen_sites with a blank
    entry — register() filtering blanks downstream is not a contract this
    method should rely on."""
    searcher = _searcher(logger, [_candidate(price=100.0, url="not a url")])
    searcher.price_market(ITEM, None, NOW)
    assert "" not in searcher.seen_sites
