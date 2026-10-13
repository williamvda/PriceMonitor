"""Tests for the PriceMonitor controller: timing, orchestration, and the CLI parser."""

import logging
import signal
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta
from unittest import mock

import pandas as pd
import pytest

import price_monitor.price_monitor as price_monitor_module
from price_monitor.app_config import PriceCtrl
from price_monitor.models import Detection, Item, PriceReading, PriceStatus
from price_monitor.price_monitor import PriceMonitor, args_parser
from price_monitor.search.searcher import SpendCapReached
from price_monitor.sheets.history_tab import COLUMNS as HISTORY_COLUMNS


class FakeSheet:
    def __init__(self, tabs: dict[str, pd.DataFrame]) -> None:
        self.tabs = tabs
        self.modified = False

    def read(self, sheet_name: str) -> pd.DataFrame:
        return self.tabs.get(sheet_name, pd.DataFrame()).copy()

    def write(self, sheet_name: str, df: pd.DataFrame) -> None:
        self.tabs[sheet_name] = df.copy()

    def update(self, sheet_name: str, df: pd.DataFrame) -> None:
        self.tabs[sheet_name] = df.copy()

    def is_modified(self) -> bool:
        return self.modified


class FakeMarketSearcher:
    """Returns a canned sweep, or raises if told to."""

    def __init__(self, price: float = 100.0, confidence: float | None = 0.9) -> None:
        self.price = price
        self.confidence = confidence
        self.swept: list[str] = []
        self.raises: Exception | None = None
        self.seen_last_price: float | None = None
        self.calls: list[str] = []
        self.blocked_seen: set[str] | None = None
        self.sources_sequence: list[int] = []
        self.seen_sites: set[str] = set()

    def price_market(
        self,
        item: Item,
        last_price: float | None,
        timestamp: datetime,
        blocked: set[str] | None = None,
        ratings: dict | None = None,
    ) -> PriceReading:
        self.swept.append(item.name)
        self.calls.append(item.name)
        self.blocked_seen = blocked
        self.seen_last_price = last_price
        if self.raises is not None:
            raise self.raises
        return PriceReading(
            timestamp=timestamp,
            item=item.name,
            currency="GBP",
            status=PriceStatus.OK,
            best_rated=Detection(self.price + 40.0, "john-lewis.com", "4.6 Trustpilot"),
            cheapest=Detection(self.price, "argos.co.uk", "4.4 Trustpilot"),
            best_value=Detection(self.price + 5.0, "very.co.uk", "4.5 Trustpilot"),
            note="3 sources, cited",
            confidence=self.confidence,
            sources=(self.sources_sequence.pop(0) if self.sources_sequence else 3),
        )


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("test")


@pytest.fixture
def searcher() -> FakeMarketSearcher:
    return FakeMarketSearcher()


def _monitor(
    sheet: FakeSheet, searcher: FakeMarketSearcher, logger: logging.Logger
) -> PriceMonitor:
    return PriceMonitor.for_test(gsheet=sheet, searcher=searcher, logger=logger)


def _items(names: list[str]) -> pd.DataFrame:
    return pd.DataFrame([{"item": n} for n in names], columns=["item"])


# Expressed relative to the configured window, not as a magic number of hours:
# a test that hardcodes "7 hours ago" silently stops testing staleness the day
# refresh_rate_h changes.
_REFRESH_H = PriceCtrl().refresh_rate_h
_STALE = timedelta(hours=_REFRESH_H + 1)
_FRESH = timedelta(hours=_REFRESH_H / 2)


def _history_row(
    item: str,
    when: datetime,
    price: float | None = 100.0,
    status: str = "ok",
    sources: str = "3",
) -> dict[str, str]:
    cell = "" if price is None else f"{price:.2f}"
    return {
        "timestamp": when.strftime("%Y-%m-%d %H:%M:%S"),
        "item": item,
        "rated_price": cell,
        "rated_site": "argos.co.uk",
        "rated_rating": "4.4 Trustpilot",
        "cheapest_price": cell,
        "cheapest_site": "argos.co.uk",
        "cheapest_rating": "4.4 Trustpilot",
        "value_price": cell,
        "value_site": "argos.co.uk",
        "value_rating": "4.4 Trustpilot",
        "confidence": "0.90",
        "sources": sources,
        "note": "",
        "status": status,
    }


def _sheet_with_history(rows: list[dict[str, str]], items: list[str]) -> FakeSheet:
    return FakeSheet(
        {
            "Items": _items(items),
            "Prices": pd.DataFrame(rows, columns=HISTORY_COLUMNS),
        }
    )


def test_update_sweeps_every_item_once(logger, searcher):
    """One sweep per item is structural now: there is no second lookup to
    de-duplicate against."""
    sheet = FakeSheet({"Items": _items(["Widget", "Gadget"])})
    _monitor(sheet, searcher, logger).update()
    assert searcher.swept == ["Widget", "Gadget"]
    assert len(sheet.tabs["Prices"]) == 2


def test_a_repeated_item_name_is_swept_only_once(logger, searcher):
    """The key is the name alone, so a duplicated row must not bill a second
    sweep or double-count the item in its own history."""
    sheet = FakeSheet({"Items": _items(["Widget", "Widget"])})
    _monitor(sheet, searcher, logger).update()
    assert searcher.swept == ["Widget"]
    assert len(sheet.tabs["Prices"]) == 1


def test_update_writes_the_summary_back(logger, searcher):
    sheet = FakeSheet({"Items": _items(["Widget"])})
    _monitor(sheet, searcher, logger).update()
    row = sheet.tabs["Items"].iloc[0]
    assert row["cheapest_price"] == 100.0
    assert row["cheapest_site"] == "argos.co.uk"
    assert row["rated_site"] == "john-lewis.com"
    assert row["value_site"] == "very.co.uk"
    assert row["sources"] == "3"


def test_poll_does_nothing_when_the_sheet_is_unchanged(logger, searcher):
    sheet = FakeSheet({"Items": _items(["Widget"])})
    monitor = _monitor(sheet, searcher, logger)
    sheet.modified = False
    monitor.poll()
    assert searcher.swept == []


def test_poll_sweeps_only_the_new_item(logger, searcher):
    old = datetime.now() - _FRESH
    sheet = _sheet_with_history([_history_row("Widget", old)], ["Widget", "Gadget"])
    monitor = _monitor(sheet, searcher, logger)
    sheet.modified = True
    monitor.poll()
    assert searcher.swept == ["Gadget"]


def test_a_failing_sweep_does_not_stop_the_others(logger):
    class Exploding(FakeMarketSearcher):
        def price_market(self, item, last_price, timestamp, blocked=None, ratings=None):
            if item.name == "Widget":
                raise RuntimeError("boom")
            return super().price_market(item, last_price, timestamp)

    sheet = FakeSheet({"Items": _items(["Widget", "Gadget"])})
    _monitor(sheet, Exploding(), logger).update()
    statuses = list(sheet.tabs["Prices"]["status"])
    assert "error" in statuses
    assert "ok" in statuses


def test_a_failing_sweep_records_no_detections(logger, searcher):
    searcher.raises = RuntimeError("sweep exploded")
    sheet = FakeSheet({"Items": _items(["Widget"])})
    _monitor(sheet, searcher, logger).update()
    row = sheet.tabs["Prices"].iloc[0]
    assert row["status"] == PriceStatus.ERROR.value
    assert row["cheapest_price"] == ""
    assert row["rated_price"] == ""
    assert row["value_price"] == ""


def test_the_last_cheapest_price_is_passed_to_the_searcher(logger, searcher):
    old = datetime.now() - _STALE
    sheet = _sheet_with_history([_history_row("Widget", old, price=80.0)], ["Widget"])
    _monitor(sheet, searcher, logger).update()
    assert searcher.seen_last_price == 80.0


def test_run_survives_a_failing_update_and_stop_is_prompt(logger, searcher):
    sheet = FakeSheet({"Items": _items(["Widget"])})
    monitor = _monitor(sheet, searcher, logger)
    # Short intervals so a surviving loop calls update() again almost at
    # once. A *second* call is the real proof _safe_run's except clause
    # caught the first exception — a single call is consistent with either
    # a working loop or one that quietly died right after it.
    monitor.ctrl = PriceCtrl(refresh_rate_h=1e-7, poll_rate_m=1e-7, request_delay_s=0.0)

    calls = 0
    calls_lock = threading.Lock()
    second_call = threading.Event()

    def failing_update() -> None:
        nonlocal calls
        with calls_lock:
            calls += 1
            reached_two = calls >= 2
        if reached_two:
            second_call.set()
        raise RuntimeError("boom")

    monitor.update = failing_update

    monitor.start()
    try:
        assert second_call.wait(timeout=2.0), (
            f"update() was called only {calls} time(s) within the timeout — "
            "_safe_run must have stopped catching the exception"
        )
        # The thread must still be running here: it survived at least one
        # exception rather than dying with it. This is the assertion a
        # thread that died on the first exception would fail.
        assert monitor.thread.is_alive()

        started = time.monotonic()
        monitor.stop()
        elapsed = time.monotonic() - started
    finally:
        if monitor.thread.is_alive():
            monitor.stop_event.set()
            monitor.thread.join(timeout=2.0)

    assert not monitor.thread.is_alive()
    assert elapsed < 5.0  # generous bound: stop() must not hang


def test_parser_requires_secrets():
    with pytest.raises(SystemExit):
        args_parser().parse_args([])


def test_parser_accepts_once_flag():
    args = args_parser().parse_args(["--secrets", "/tmp/s", "--once"])
    assert args.once is True
    assert str(args.secrets) == "/tmp/s"


def test_update_skips_an_item_checked_within_the_refresh_window(logger, searcher):
    """Each sweep costs a billed search, so a recent check holds off the next."""
    recent = datetime.now() - _FRESH
    sheet = _sheet_with_history([_history_row("Widget", recent)], ["Widget"])
    _monitor(sheet, searcher, logger).update()
    assert searcher.swept == []


def test_update_sweeps_an_item_checked_before_the_refresh_window(logger, searcher):
    stale = datetime.now() - _STALE
    sheet = _sheet_with_history([_history_row("Widget", stale)], ["Widget"])
    _monitor(sheet, searcher, logger).update()
    assert searcher.swept == ["Widget"]


def test_update_sweeps_an_item_with_no_history_at_all(logger, searcher):
    recent = datetime.now() - _FRESH
    sheet = _sheet_with_history([_history_row("Widget", recent)], ["Widget", "Gadget"])
    _monitor(sheet, searcher, logger).update()
    assert searcher.swept == ["Gadget"]


def test_force_re_sweeps_everything_regardless_of_recency(logger, searcher):
    recent = datetime.now() - _FRESH
    sheet = _sheet_with_history([_history_row("Widget", recent)], ["Widget"])
    _monitor(sheet, searcher, logger).update(force=True)
    assert searcher.swept == ["Widget"]


def test_a_failed_check_still_holds_off_the_next_one(logger, searcher):
    """A not_found sweep spent an API call just as an ok one did, so it must
    count as a check — otherwise a failing item is retried on every run."""
    recent = datetime.now() - _FRESH
    row = _history_row("Widget", recent, price=None, status="not_found")
    sheet = _sheet_with_history([row], ["Widget"])
    _monitor(sheet, searcher, logger).update()
    assert searcher.swept == []


def test_recency_uses_the_newest_row_not_the_last_one(logger, searcher):
    """Rows can land out of order; the most recent check is what matters."""
    rows = [
        _history_row("Widget", datetime.now() - _FRESH),
        _history_row("Widget", datetime.now() - timedelta(hours=9)),
    ]
    _monitor(_sheet_with_history(rows, ["Widget"]), searcher, logger).update()
    assert searcher.swept == []


def test_an_unparseable_timestamp_does_not_suppress_a_refresh(logger, searcher):
    """A hand-edited sheet should cause an extra lookup, never a silent skip."""
    row = _history_row("Widget", datetime.now())
    row["timestamp"] = "yesterday-ish"
    sheet = _sheet_with_history([row], ["Widget"])
    _monitor(sheet, searcher, logger).update()
    assert searcher.swept == ["Widget"]


def test_skipping_every_item_writes_nothing(logger, searcher):
    recent = datetime.now() - _FRESH
    sheet = _sheet_with_history([_history_row("Widget", recent)], ["Widget"])
    before = len(sheet.tabs["Prices"])
    _monitor(sheet, searcher, logger).update()
    assert len(sheet.tabs["Prices"]) == before


def test_poll_still_sweeps_a_brand_new_item(logger, searcher):
    """poll() handles items with no history, so the recency filter must not
    reach it — a newly added item is priced now, not at the next refresh."""
    recent = datetime.now() - _FRESH
    sheet = _sheet_with_history([_history_row("Widget", recent)], ["Widget", "Gadget"])
    sheet.modified = True
    _monitor(sheet, searcher, logger).poll()
    assert searcher.swept == ["Gadget"]


def test_parser_accepts_force():
    args = args_parser().parse_args(["--secrets", "/tmp/s", "--once", "--force"])
    assert args.force is True


def test_force_defaults_to_off():
    assert args_parser().parse_args(["--secrets", "/tmp/s"]).force is False


def test_sigterm_handler_sets_the_stop_event(logger, searcher):
    """A service manager stops the process with SIGTERM, which Python does not
    catch by default — without this the worker thread dies mid-write."""
    monitor = _monitor(FakeSheet({"Items": _items([])}), searcher, logger)
    installed: dict[int, object] = {}

    def fake_signal(signum, handler):
        installed[signum] = handler

    with mock.patch.object(price_monitor_module.signal, "signal", fake_signal):
        price_monitor_module._install_sigterm_handler(monitor, logger)

    assert signal.SIGTERM in installed
    assert not monitor.stop_event.is_set()
    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert monitor.stop_event.is_set()


# --- MsgServer notifications -----------------------------------------------


class RecordingNotifier:
    """Captures notify() calls in place of a real MsgServer client."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    @property
    def enabled(self) -> bool:
        return True

    def notify(self, text: str) -> None:
        self.messages.append(text)


def _monitor_with_notifier(
    sheet: FakeSheet, searcher: FakeMarketSearcher, logger: logging.Logger
) -> tuple[PriceMonitor, RecordingNotifier]:
    monitor = _monitor(sheet, searcher, logger)
    notifier = RecordingNotifier()
    monitor.notifier = notifier
    return monitor, notifier


def test_for_test_monitor_has_notifications_disabled(logger, searcher):
    monitor = _monitor(FakeSheet({"Items": _items(["Widget"])}), searcher, logger)
    assert monitor.msg_client is None
    assert monitor.notifier.enabled is False
    monitor.update()  # the no-op notifier must not get in the way


def test_update_sends_only_a_complete_notice(logger, searcher):
    sheet = FakeSheet({"Items": _items(["Widget", "Gadget"])})
    monitor, notifier = _monitor_with_notifier(sheet, searcher, logger)
    monitor.update()

    assert notifier.messages == [
        "✅ PriceMonitor update complete — 2/2 priced, 0 failed",
    ]


def test_a_failing_sweep_is_reported_as_a_failure(logger, searcher):
    searcher.raises = RuntimeError("search down")
    sheet = FakeSheet({"Items": _items(["Widget"])})
    monitor, notifier = _monitor_with_notifier(sheet, searcher, logger)
    monitor.update()

    assert notifier.messages[-1] == (
        "❌ PriceMonitor update complete — 0/1 priced, 1 failed"
    )


def test_a_check_that_aborts_still_sends_a_failure_notice(logger, searcher):
    sheet = FakeSheet({"Items": _items(["Widget"])})
    monitor, notifier = _monitor_with_notifier(sheet, searcher, logger)
    monitor.history.append = mock.Mock(side_effect=RuntimeError("sheet unreachable"))

    with pytest.raises(RuntimeError):
        monitor.update()

    assert notifier.messages[-1] == (
        "❌ PriceMonitor update failed — sheet unreachable"
    )


def test_an_aborted_check_is_logged_and_retried_by_the_loop(logger, searcher):
    """_safe_run must still see the exception the notifier re-raised."""
    sheet = FakeSheet({"Items": _items(["Widget"])})
    monitor, notifier = _monitor_with_notifier(sheet, searcher, logger)
    monitor.history.append = mock.Mock(side_effect=RuntimeError("sheet unreachable"))

    monitor._safe_run("Update", monitor.update)  # must not propagate

    assert notifier.messages[-1].startswith("❌ PriceMonitor update failed")


def test_a_check_with_nothing_due_says_nothing(logger, searcher):
    sheet = FakeSheet({"Items": _items([])})
    monitor, notifier = _monitor_with_notifier(sheet, searcher, logger)
    monitor.update()
    assert notifier.messages == []


def test_an_idle_poll_says_nothing(logger, searcher):
    sheet = FakeSheet({"Items": _items(["Widget"])})
    monitor, notifier = _monitor_with_notifier(sheet, searcher, logger)
    sheet.modified = False
    monitor.poll()
    assert notifier.messages == []


def test_a_poll_is_labelled_poll_not_update(logger, searcher):
    sheet = FakeSheet({"Items": _items(["Widget"])})
    monitor, notifier = _monitor_with_notifier(sheet, searcher, logger)
    sheet.modified = True
    monitor.poll()

    assert notifier.messages == [
        "✅ PriceMonitor poll complete — 1/1 priced, 0 failed",
    ]


def test_the_spend_cap_stops_the_check_and_reports_once(logger, searcher, caplog):
    searcher.raises = SpendCapReached("monthly spending cap")
    sheet = FakeSheet({"Items": _items(["Widget", "Gadget"])})
    monitor, notifier = _monitor_with_notifier(sheet, searcher, logger)

    with caplog.at_level(logging.WARNING):
        monitor.update()

    assert searcher.swept == ["Widget"]
    assert notifier.messages == []
    assert [r.message for r in caplog.records if "spending cap" in r.message] == [
        caplog.records[-1].message
    ]
    assert monitor.paused_until is not None


def test_the_spend_cap_pause_ends_at_noon_on_the_next_month(logger, searcher):
    class December(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 12, 14, 9, 30)

    monitor = _monitor(FakeSheet({"Items": _items([])}), searcher, logger)
    with mock.patch.object(price_monitor_module, "datetime", December):
        monitor._pause(SpendCapReached("cap"))
    assert monitor.paused_until == datetime(2027, 1, 1, 12, 0)


def test_an_update_command_ends_a_pause_and_forces_an_update(logger, searcher):
    monitor = _monitor(FakeSheet({"Items": _items([])}), searcher, logger)
    monitor.paused_until = datetime.now() + timedelta(days=10)
    forced: list[bool] = []
    updated = threading.Event()

    def recording_update(force: bool = False) -> None:
        forced.append(force)
        updated.set()

    monitor.update = recording_update
    monitor.update_requested.set()
    monitor.thread.start()
    try:
        assert updated.wait(timeout=2.0)
    finally:
        monitor.stop()

    assert forced[0] is True
    assert monitor.paused_until is None


def test_a_pause_command_pauses_until_next_month(logger, searcher):
    monitor = _monitor(FakeSheet({"Items": _items([])}), searcher, logger)
    reply = monitor._pause_by_command()
    assert monitor.paused_until is not None
    assert monitor.paused_until.day == 1
    assert reply == f"⏸ PriceMonitor paused until {monitor.paused_until:%Y-%m-%d %H:%M}"


def test_update_skips_items_with_search_off(logger, searcher):
    items = pd.DataFrame([{"item": "Widget", "search": "FALSE"}, {"item": "Gadget"}])
    sheet = FakeSheet({"Items": items})
    monitor = _monitor(sheet, searcher, logger)
    monitor.update(force=True)
    assert searcher.swept == ["Gadget"]
    # Still listed: the summary rewrite must not drop an item that was skipped.
    assert set(sheet.tabs["Items"]["item"]) == {"Widget", "Gadget"}


def test_poll_skips_a_new_item_with_search_off(logger, searcher):
    items = pd.DataFrame([{"item": "Widget", "search": "FALSE"}])
    sheet = FakeSheet({"Items": items})
    sheet.modified = True
    _monitor(sheet, searcher, logger).poll()
    assert searcher.swept == []


def test_parser_accepts_msg_server_flag():
    assert args_parser().parse_args(["--secrets", "/tmp/s", "--msg-server"]).msg_server
    assert args_parser().parse_args(["--secrets", "/tmp/s"]).msg_server is False


def test_once_still_tears_down_the_msg_client(monkeypatch):
    """--once returns early; the finally must still close the forwarder."""
    monitor = mock.Mock()
    monkeypatch.setattr(
        price_monitor_module, "PriceMonitor", mock.Mock(return_value=monitor)
    )
    monkeypatch.setattr("sys.argv", ["price-monitor", "--secrets", "/tmp/s", "--once"])

    price_monitor_module.main()

    monitor.update.assert_called_once_with(force=False)
    monitor.stop.assert_called_once()


# --- price-drop notifications ----------------------------------------------


def _seed(item: str, prices: list[float]) -> list[dict[str, str]]:
    """Prior priced sweeps for ``item`` — new_drop_band stays silent below
    PriceCtrl.min_readings, so a drop test needs a settled baseline."""
    old = datetime.now() - _STALE
    return [
        _history_row(item, old - timedelta(hours=len(prices) - i), price=price)
        for i, price in enumerate(prices)
    ]


def _drops(notifier: RecordingNotifier) -> list[str]:
    return [m for m in notifier.messages if m.startswith("📉")]


def test_a_price_crossing_below_the_mean_is_announced(logger):
    """The baseline is read before the append, so the new low price is compared
    against the mean of what came before it, not one it has already pulled down."""
    sheet = _sheet_with_history(_seed("Widget", [110.0, 120.0, 130.0]), ["Widget"])
    monitor, notifier = _monitor_with_notifier(
        sheet, FakeMarketSearcher(price=90.0), logger
    )
    monitor.update()

    assert notifier.messages[-1] == (
        "📉 PriceMonitor — Widget: 90.00 GBP at argos.co.uk "
        "(4.4 Trustpilot, confidence 0.90), below its 120.00 GBP mean (-25%)"
    )


def test_a_price_that_stays_below_the_mean_is_not_re_announced(logger):
    """Six hours later the price is still low; the drop was already reported."""
    sheet = _sheet_with_history(_seed("Widget", [130.0, 120.0, 90.0]), ["Widget"])
    monitor, notifier = _monitor_with_notifier(
        sheet, FakeMarketSearcher(price=89.0), logger
    )
    monitor.update()

    assert _drops(notifier) == []


def test_an_item_with_too_little_history_announces_nothing(logger):
    sheet = _sheet_with_history(_seed("Widget", [130.0, 110.0]), ["Widget"])
    monitor, notifier = _monitor_with_notifier(
        sheet, FakeMarketSearcher(price=90.0), logger
    )
    monitor.update()

    assert _drops(notifier) == []


def test_a_sweep_that_found_no_price_announces_nothing(logger, searcher):
    """A reading with no cheapest detection has nothing to compare."""
    searcher.raises = RuntimeError("search down")
    sheet = _sheet_with_history(_seed("Widget", [130.0, 120.0, 110.0]), ["Widget"])
    monitor, notifier = _monitor_with_notifier(sheet, searcher, logger)
    monitor.update()

    assert _drops(notifier) == []


def test_a_slow_slide_past_the_next_step_is_announced_again(logger):
    """The price already crossed below the mean; giving up another step must
    still report, or a long steady decline goes silent after one message."""
    sheet = _sheet_with_history(_seed("Widget", [100.0, 100.0, 95.0]), ["Widget"])
    monitor, notifier = _monitor_with_notifier(
        sheet, FakeMarketSearcher(price=88.0), logger
    )
    monitor.update()

    assert notifier.messages[-1] == (
        "📉 PriceMonitor — Widget: 88.00 GBP at argos.co.uk "
        "(4.4 Trustpilot, confidence 0.90), still falling, "
        "below its 98.33 GBP mean (-11%)"
    )


def test_a_fall_that_stays_inside_its_step_is_not_announced_again(logger):
    sheet = _sheet_with_history(_seed("Widget", [100.0, 100.0, 95.0]), ["Widget"])
    monitor, notifier = _monitor_with_notifier(
        sheet, FakeMarketSearcher(price=94.0), logger
    )
    monitor.update()

    assert _drops(notifier) == []


def test_a_sharp_first_crossing_is_not_worded_as_still_falling(logger):
    """Landing several steps down in one go is still a first drop, not a slide."""
    sheet = _sheet_with_history(_seed("Widget", [110.0, 120.0, 130.0]), ["Widget"])
    monitor, notifier = _monitor_with_notifier(
        sheet, FakeMarketSearcher(price=90.0), logger
    )
    monitor.update()

    assert "still falling" not in notifier.messages[-1]


# --- confidence gate ---------------------------------------------------------


def test_a_low_confidence_drop_is_recorded_but_not_announced(logger, caplog):
    sheet = _sheet_with_history(_seed("Widget", [200.0, 200.0, 200.0]), ["Widget"])
    monitor, notifier = _monitor_with_notifier(
        sheet, FakeMarketSearcher(price=100.0, confidence=0.4), logger
    )
    caplog.clear()
    with caplog.at_level(logging.INFO):
        monitor.update(force=True)

    prices = sheet.tabs["Prices"]
    assert "100.00" in list(prices["cheapest_price"])
    assert _drops(notifier) == []
    # The suppression must be logged, not silent: an item gated on every run
    # is a prompt problem worth seeing, and a refactor that deleted the
    # logging call would otherwise leave this test green.
    assert any(
        "Widget" in record.message and "0.40" in record.message
        for record in caplog.records
    )


def test_a_drop_with_no_confidence_is_not_announced(logger):
    """Unreachable via `_rank` today (it always sets a confidence), but the
    gate must not rely on that: a `cheapest` with no `confidence` must be
    suppressed, not passed on to a formatter that would raise on `None`."""
    sheet = _sheet_with_history(_seed("Widget", [200.0, 200.0, 200.0]), ["Widget"])
    monitor, notifier = _monitor_with_notifier(
        sheet, FakeMarketSearcher(price=100.0, confidence=None), logger
    )
    monitor.update(force=True)

    assert _drops(notifier) == []


def test_a_confident_drop_is_announced(logger):
    sheet = _sheet_with_history(_seed("Widget", [200.0, 200.0, 200.0]), ["Widget"])
    monitor, notifier = _monitor_with_notifier(
        sheet, FakeMarketSearcher(price=100.0, confidence=0.9), logger
    )
    monitor.update(force=True)

    assert any("argos.co.uk" in message for message in _drops(notifier))


def test_a_renamed_item_is_warned_about_not_silently_restarted(
    logger, searcher, caplog
):
    """The item name is the only identity there is, so a rename orphans the
    old history and restarts min/mean. It cannot be reconciled — a renamed
    item and a deleted one look identical — so it must at least be said."""
    sheet = _sheet_with_history(
        [_history_row("WeeRide Safe Deluxe", datetime.now() - _STALE)],
        ["WeeRide Front Safe Deluxe"],
    )
    with caplog.at_level(logging.WARNING):
        _monitor(sheet, searcher, logger).update()

    warning = "\n".join(r.message for r in caplog.records)
    assert "WeeRide Safe Deluxe" in warning
    assert "no longer on the" in warning


def test_no_orphan_warning_when_every_item_still_matches(logger, searcher, caplog):
    sheet = _sheet_with_history(
        [_history_row("Widget", datetime.now() - _STALE)], ["Widget"]
    )
    with caplog.at_level(logging.WARNING):
        _monitor(sheet, searcher, logger).update()
    assert "no longer on the" not in "\n".join(r.message for r in caplog.records)


def test_a_thin_sweep_is_retried_against_the_items_own_recent_best(logger):
    """WeeRide returned 3, 3, then 1 sources for the same query. A sweep is
    judged against what that item usually manages, not a fixed bar."""
    sheet = _sheet_with_history(
        [_history_row("Widget", datetime.now() - _STALE, sources="3")], ["Widget"]
    )
    searcher = FakeMarketSearcher()
    searcher.sources_sequence = [1, 3]
    _monitor(sheet, searcher, logger).update()
    assert len(searcher.calls) == 2, "a thin result should be swept once more"


def test_only_the_better_of_the_two_sweeps_is_recorded(logger):
    """Appending both would double the item's history and drag its mean
    toward a result we already believe is wrong."""
    sheet = _sheet_with_history(
        [_history_row("Widget", datetime.now() - _STALE, sources="3")], ["Widget"]
    )
    searcher = FakeMarketSearcher()
    searcher.sources_sequence = [1, 3]
    _monitor(sheet, searcher, logger).update()
    appended = sheet.tabs["Prices"]
    assert len(appended) == 2, "one prior row plus one new one"
    assert appended.iloc[-1]["sources"] == "3"


def test_a_genuinely_single_source_item_is_never_retried(logger):
    """Its own history says one retailer is all there is, so nothing is wrong."""
    sheet = _sheet_with_history(
        [_history_row("Widget", datetime.now() - _STALE, sources="1")], ["Widget"]
    )
    searcher = FakeMarketSearcher()
    searcher.sources_sequence = [1, 1]
    _monitor(sheet, searcher, logger).update()
    assert len(searcher.calls) == 1


def test_the_retry_can_be_turned_off(logger):
    """It spends real money on a guess, so it must be switchable."""
    sheet = _sheet_with_history(
        [_history_row("Widget", datetime.now() - _STALE, sources="3")], ["Widget"]
    )
    searcher = FakeMarketSearcher()
    searcher.sources_sequence = [1, 3]
    monitor = _monitor(sheet, searcher, logger)
    monitor.ctrl = replace(monitor.ctrl, resweep_thin=False)
    monitor.update()
    assert len(searcher.calls) == 1


def test_blocked_sites_reach_the_searcher(logger):
    sheet = _sheet_with_history(
        [_history_row("Widget", datetime.now() - _STALE)], ["Widget"]
    )
    sheet.tabs["Control"] = pd.DataFrame({"blocked": ["amazon.co.uk"]})
    searcher = FakeMarketSearcher()
    _monitor(sheet, searcher, logger).update()
    assert searcher.blocked_seen == {"amazon.co.uk"}


# --- ratings cache -----------------------------------------------------------


class _FakeRatingsTab:
    def __init__(self, stale=None, raises=None, on_call=None):
        self._stale = stale if stale is not None else []
        self._raises = raises
        self._on_call = on_call or (lambda _name: None)
        self.registered: set[str] = set()
        self.stored: dict = {}

    def read(self):
        if self._raises is not None:
            raise self._raises
        return {}

    def stale(self, within_days):
        if self._raises is not None:
            raise self._raises
        return self._stale

    def register(self, sites):
        self._on_call("register")
        self.registered = set(sites)

    def store(self, results):
        self.stored = results


class _FakeLookup:
    def __init__(self, on_call=None):
        self._on_call = on_call or (lambda _name: None)
        self.fetched: list[list[str]] = []

    def fetch(self, sites):
        self._on_call("fetch")
        self.fetched.append(list(sites))
        return {}


def test_stale_ratings_are_refreshed_before_the_sweeps(logger):
    """The sweep needs the cache, so filling it afterwards would waste a day."""
    order: list[str] = []
    sheet = _sheet_with_history(
        [_history_row("Widget", datetime.now() - _STALE)], ["Widget"]
    )
    searcher = FakeMarketSearcher()
    # Registration is skipped on an empty set (it costs a Sheets read for
    # nothing), so this needs a real site for "register" to actually fire.
    searcher.seen_sites = {"argos.co.uk"}
    monitor = _monitor(sheet, searcher, logger)
    monitor.ratings_tab = _FakeRatingsTab(stale=["argos.co.uk"], on_call=order.append)
    monitor.ratings_lookup = _FakeLookup(on_call=order.append)
    monitor.update()
    assert order.index("fetch") < order.index("register")


def test_sites_the_sweep_saw_are_registered_afterwards(logger):
    sheet = _sheet_with_history(
        [_history_row("Widget", datetime.now() - _STALE)], ["Widget"]
    )
    searcher = FakeMarketSearcher()
    searcher.seen_sites = {"argos.co.uk", "currys.co.uk"}
    monitor = _monitor(sheet, searcher, logger)
    tab = _FakeRatingsTab()
    monitor.ratings_tab = tab
    monitor.update()
    assert tab.registered == {"argos.co.uk", "currys.co.uk"}


def test_nothing_stale_makes_no_lookup(logger):
    sheet = _sheet_with_history(
        [_history_row("Widget", datetime.now() - _STALE)], ["Widget"]
    )
    monitor = _monitor(sheet, FakeMarketSearcher(), logger)
    monitor.ratings_tab = _FakeRatingsTab(stale=[])
    lookup = _FakeLookup()
    monitor.ratings_lookup = lookup
    monitor.update()
    assert lookup.fetched == []


def test_only_one_batch_is_looked_up_per_run(logger):
    """A first run against fifty retailers must not spend fifty searches."""
    sheet = _sheet_with_history(
        [_history_row("Widget", datetime.now() - _STALE)], ["Widget"]
    )
    monitor = _monitor(sheet, FakeMarketSearcher(), logger)
    monitor.ratings_tab = _FakeRatingsTab(stale=[f"shop{n}.co.uk" for n in range(50)])
    lookup = _FakeLookup()
    monitor.ratings_lookup = lookup
    monitor.update()
    assert len(lookup.fetched[0]) == 10


def test_a_ratings_failure_does_not_stop_the_sweep(logger):
    """Ratings are a nicety; prices are the job."""
    sheet = _sheet_with_history(
        [_history_row("Widget", datetime.now() - _STALE)], ["Widget"]
    )
    searcher = FakeMarketSearcher()
    monitor = _monitor(sheet, searcher, logger)
    monitor.ratings_tab = _FakeRatingsTab(
        stale=["argos.co.uk"], raises=RuntimeError("sheet down")
    )
    monitor.update()
    assert searcher.calls == ["Widget"]


def test_a_failed_items_sites_do_not_clobber_a_prior_items_registration(logger):
    """test_market.py proves seen_sites resets to empty on a failed sweep;
    test_every_items_seen_sites_are_registered_not_just_the_last proves sites
    accumulate across successful items. Neither covers the intersection: a
    run where item 1 succeeds and item 2 fails must still register item 1's
    retailers, and nothing from item 2."""

    class PerItemSearcher(FakeMarketSearcher):
        def price_market(self, item, last_price, timestamp, blocked=None, ratings=None):
            if item.name == "Widget":
                self.seen_sites = {"argos.co.uk"}
                return super().price_market(
                    item, last_price, timestamp, blocked, ratings
                )
            # Mirrors MarketSearcher's real behaviour: a failed sweep resets
            # seen_sites to empty before this caller ever sees it.
            self.seen_sites = set()
            return PriceReading(
                timestamp=timestamp,
                item=item.name,
                currency="GBP",
                status=PriceStatus.PARSE_ERROR,
            )

    sheet = FakeSheet({"Items": _items(["Widget", "Gadget"])})
    searcher = PerItemSearcher()
    monitor = _monitor(sheet, searcher, logger)
    tab = _FakeRatingsTab()
    monitor.ratings_tab = tab
    monitor.update()
    assert tab.registered == {"argos.co.uk"}


def test_every_items_seen_sites_are_registered_not_just_the_last(logger):
    """seen_sites resets on every sweep, so registering once after the loop
    must accumulate across items, not just read the last item's leftovers."""

    class PerItemSearcher(FakeMarketSearcher):
        def price_market(self, item, last_price, timestamp, blocked=None, ratings=None):
            reading = super().price_market(
                item, last_price, timestamp, blocked, ratings
            )
            self.seen_sites = {f"{item.name.lower()}-shop.co.uk"}
            return reading

    sheet = FakeSheet({"Items": _items(["Widget", "Gadget"])})
    searcher = PerItemSearcher()
    monitor = _monitor(sheet, searcher, logger)
    tab = _FakeRatingsTab()
    monitor.ratings_tab = tab
    monitor.update()
    assert tab.registered == {"widget-shop.co.uk", "gadget-shop.co.uk"}
