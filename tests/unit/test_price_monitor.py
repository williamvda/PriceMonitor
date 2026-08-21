"""Tests for the PriceMonitor controller: timing, orchestration, and the CLI parser."""

import logging
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import pytest

from price_monitor.app_config import PriceCtrl
from price_monitor.models import Item, PriceReading, PriceStatus
from price_monitor.price_monitor import PriceMonitor, args_parser


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


class FakeSearcher:
    def __init__(self) -> None:
        self.priced: list[str] = []

    def price(
        self, item: Item, last_price: float | None, timestamp: datetime
    ) -> PriceReading:
        self.priced.append(item.name)
        return PriceReading(
            timestamp=timestamp,
            item=item.name,
            website=item.website,
            price=100.0,
            currency="GBP",
            status=PriceStatus.OK,
            source_url="https://shop.example/w",
        )


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("test")


def _monitor(
    sheet: FakeSheet, searcher: FakeSearcher, logger: logging.Logger
) -> PriceMonitor:
    return PriceMonitor.for_test(gsheet=sheet, searcher=searcher, logger=logger)


def _items(rows: list[dict[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["item", "website"])


def test_update_prices_every_item(logger):
    sheet = FakeSheet(
        {
            "Items": _items(
                [
                    {"item": "Widget", "website": "shop.example"},
                    {"item": "Gadget", "website": "shop.example"},
                ]
            )
        }
    )
    searcher = FakeSearcher()
    _monitor(sheet, searcher, logger).update()
    assert searcher.priced == ["Widget", "Gadget"]
    assert len(sheet.tabs["Prices"]) == 2


def test_update_writes_the_summary_back(logger):
    sheet = FakeSheet(
        {"Items": _items([{"item": "Widget", "website": "shop.example"}])}
    )
    _monitor(sheet, FakeSearcher(), logger).update()
    assert sheet.tabs["Items"].iloc[0]["current"] == 100.0


def test_poll_does_nothing_when_the_sheet_is_unchanged(logger):
    sheet = FakeSheet(
        {"Items": _items([{"item": "Widget", "website": "shop.example"}])}
    )
    searcher = FakeSearcher()
    monitor = _monitor(sheet, searcher, logger)
    sheet.modified = False
    monitor.poll()
    assert searcher.priced == []


def test_poll_prices_only_the_new_item(logger):
    sheet = FakeSheet(
        {
            "Items": _items(
                [
                    {"item": "Widget", "website": "shop.example"},
                    {"item": "Gadget", "website": "shop.example"},
                ]
            ),
            "Prices": pd.DataFrame(
                [
                    {
                        "timestamp": "2026-08-20 06:00:00",
                        "item": "Widget",
                        "website": "shop.example",
                        "price": "100.00",
                        "currency": "GBP",
                        "status": "ok",
                        "source_url": "",
                        "note": "",
                    }
                ],
                columns=[
                    "timestamp",
                    "item",
                    "website",
                    "price",
                    "currency",
                    "status",
                    "source_url",
                    "note",
                ],
            ),
        }
    )
    searcher = FakeSearcher()
    monitor = _monitor(sheet, searcher, logger)
    sheet.modified = True
    monitor.poll()
    assert searcher.priced == ["Gadget"]


def test_a_failing_item_does_not_stop_the_others(logger):
    class Exploding(FakeSearcher):
        def price(
            self, item: Item, last_price: float | None, timestamp: datetime
        ) -> PriceReading:
            if item.name == "Widget":
                raise RuntimeError("boom")
            return super().price(item, last_price, timestamp)

    sheet = FakeSheet(
        {
            "Items": _items(
                [
                    {"item": "Widget", "website": "shop.example"},
                    {"item": "Gadget", "website": "shop.example"},
                ]
            )
        }
    )
    searcher = Exploding()
    _monitor(sheet, searcher, logger).update()
    statuses = list(sheet.tabs["Prices"]["status"])
    assert "error" in statuses
    assert "ok" in statuses


def test_last_price_is_passed_to_the_searcher(logger):
    seen = {}

    class Recording(FakeSearcher):
        def price(
            self, item: Item, last_price: float | None, timestamp: datetime
        ) -> PriceReading:
            seen[item.name] = last_price
            return super().price(item, last_price, timestamp)

    sheet = FakeSheet(
        {
            "Items": _items([{"item": "Widget", "website": "shop.example"}]),
            "Prices": pd.DataFrame(
                [
                    {
                        "timestamp": "2026-08-20 06:00:00",
                        "item": "Widget",
                        "website": "shop.example",
                        "price": "80.00",
                        "currency": "GBP",
                        "status": "ok",
                        "source_url": "",
                        "note": "",
                    }
                ],
                columns=[
                    "timestamp",
                    "item",
                    "website",
                    "price",
                    "currency",
                    "status",
                    "source_url",
                    "note",
                ],
            ),
        }
    )
    _monitor(sheet, Recording(), logger).update()
    assert seen["Widget"] == 80.0


def test_run_survives_a_failing_update_and_stop_is_prompt(logger):
    sheet = FakeSheet(
        {"Items": _items([{"item": "Widget", "website": "shop.example"}])}
    )
    monitor = _monitor(sheet, FakeSearcher(), logger)
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


def _history_row(item: str, when: datetime, status: str = "ok") -> dict[str, str]:
    return {
        "timestamp": when.strftime("%Y-%m-%d %H:%M:%S"),
        "item": item,
        "website": "shop.example",
        "price": "100.00",
        "currency": "GBP",
        "source_url": "",
        "note": "",
        "status": status,
    }


def _sheet_with_history(rows: list[dict[str, str]], items: list[str]) -> FakeSheet:
    return FakeSheet(
        {
            "Items": _items([{"item": n, "website": "shop.example"} for n in items]),
            "Prices": pd.DataFrame(rows),
        }
    )


def test_update_skips_an_item_checked_within_the_refresh_window(logger):
    """Each lookup costs a billed search, so a recent check holds off the next."""
    recent = datetime.now() - timedelta(hours=1)
    sheet = _sheet_with_history([_history_row("Widget", recent)], ["Widget"])
    searcher = FakeSearcher()
    _monitor(sheet, searcher, logger).update()
    assert searcher.priced == []


def test_update_prices_an_item_checked_before_the_refresh_window(logger):
    stale = datetime.now() - timedelta(hours=7)
    sheet = _sheet_with_history([_history_row("Widget", stale)], ["Widget"])
    searcher = FakeSearcher()
    _monitor(sheet, searcher, logger).update()
    assert searcher.priced == ["Widget"]


def test_update_prices_an_item_with_no_history_at_all(logger):
    recent = datetime.now() - timedelta(hours=1)
    sheet = _sheet_with_history([_history_row("Widget", recent)], ["Widget", "Gadget"])
    searcher = FakeSearcher()
    _monitor(sheet, searcher, logger).update()
    assert searcher.priced == ["Gadget"]


def test_force_re_prices_everything_regardless_of_recency(logger):
    recent = datetime.now() - timedelta(hours=1)
    sheet = _sheet_with_history([_history_row("Widget", recent)], ["Widget"])
    searcher = FakeSearcher()
    _monitor(sheet, searcher, logger).update(force=True)
    assert searcher.priced == ["Widget"]


def test_a_failed_check_still_holds_off_the_next_one(logger):
    """A not_found lookup spent an API call just as an ok one did, so it must
    count as a check — otherwise a failing item is retried on every run."""
    recent = datetime.now() - timedelta(hours=1)
    row = _history_row("Widget", recent, status="not_found")
    row["price"] = ""
    sheet = _sheet_with_history([row], ["Widget"])
    searcher = FakeSearcher()
    _monitor(sheet, searcher, logger).update()
    assert searcher.priced == []


def test_recency_uses_the_newest_row_not_the_last_one(logger):
    """Rows can land out of order; the most recent check is what matters."""
    rows = [
        _history_row("Widget", datetime.now() - timedelta(hours=1)),
        _history_row("Widget", datetime.now() - timedelta(hours=9)),
    ]
    searcher = FakeSearcher()
    _monitor(_sheet_with_history(rows, ["Widget"]), searcher, logger).update()
    assert searcher.priced == []


def test_an_unparseable_timestamp_does_not_suppress_a_refresh(logger):
    """A hand-edited sheet should cause an extra lookup, never a silent skip."""
    row = _history_row("Widget", datetime.now())
    row["timestamp"] = "yesterday-ish"
    sheet = _sheet_with_history([row], ["Widget"])
    searcher = FakeSearcher()
    _monitor(sheet, searcher, logger).update()
    assert searcher.priced == ["Widget"]


def test_skipping_every_item_writes_nothing(logger):
    recent = datetime.now() - timedelta(hours=1)
    sheet = _sheet_with_history([_history_row("Widget", recent)], ["Widget"])
    before = len(sheet.tabs["Prices"])
    _monitor(sheet, FakeSearcher(), logger).update()
    assert len(sheet.tabs["Prices"]) == before


def test_poll_still_prices_a_brand_new_item(logger):
    """poll() handles items with no history, so the recency filter must not
    reach it — a newly added item is priced immediately, not in six hours."""
    recent = datetime.now() - timedelta(hours=1)
    sheet = _sheet_with_history([_history_row("Widget", recent)], ["Widget", "Gadget"])
    sheet.modified = True
    searcher = FakeSearcher()
    _monitor(sheet, searcher, logger).poll()
    assert searcher.priced == ["Gadget"]


def test_parser_accepts_force():
    args = args_parser().parse_args(["--secrets", "/tmp/s", "--once", "--force"])
    assert args.force is True


def test_force_defaults_to_off():
    assert args_parser().parse_args(["--secrets", "/tmp/s"]).force is False
