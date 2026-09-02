"""Tests for the append-only price history tab."""

import logging
from datetime import datetime

import pandas as pd
import pytest

from price_monitor.models import PriceReading, PriceStatus
from price_monitor.sheets.history_tab import COLUMNS, HistoryTab


class FakeSheet:
    """Stands in for GoogleSheetInterface, recording which write path was used."""

    def __init__(self, frame: pd.DataFrame | None = None):
        self.frame = frame if frame is not None else pd.DataFrame()
        self.writes: list[str] = []

    def read(self, sheet_name: str) -> pd.DataFrame:
        return self.frame.copy()

    def write(self, sheet_name: str, df: pd.DataFrame) -> None:
        self.writes.append("write")
        self.frame = df.copy()

    def update(self, sheet_name: str, df: pd.DataFrame) -> None:
        self.writes.append("update")
        self.frame = df.copy()


@pytest.fixture
def logger():
    return logging.getLogger("test")


def _reading(
    item="Widget",
    price=100.0,
    when="2026-08-20 06:00:00",
    status=PriceStatus.OK,
    source_url="https://shop.example/w",
):
    return PriceReading(
        timestamp=datetime.strptime(when, "%Y-%m-%d %H:%M:%S"),
        item=item,
        website="shop.example",
        price=price,
        currency="GBP",
        status=status,
        source_url=source_url,
    )


def _history(rows: list[dict]) -> pd.DataFrame:
    columns = [
        "timestamp",
        "item",
        "website",
        "price",
        "currency",
        "source_url",
        "note",
        "status",
    ]
    return pd.DataFrame(rows, columns=columns)


def test_first_append_uses_write_so_the_tab_is_created(logger):
    sheet = FakeSheet()
    HistoryTab(sheet, "Prices", logger).append([_reading()])
    assert sheet.writes == ["write"]
    assert len(sheet.frame) == 1


def test_subsequent_append_uses_update_to_protect_history(logger):
    existing = _history(
        [
            {
                "timestamp": "2026-08-20 06:00:00",
                "item": "Widget",
                "website": "shop.example",
                "price": "100.0",
                "currency": "GBP",
                "status": "ok",
                "source_url": "",
                "note": "",
            }
        ]
    )
    sheet = FakeSheet(existing)
    HistoryTab(sheet, "Prices", logger).append([_reading(when="2026-08-20 12:00:00")])
    assert sheet.writes == ["update"]
    assert len(sheet.frame) == 2


def test_append_of_nothing_writes_nothing(logger):
    sheet = FakeSheet()
    HistoryTab(sheet, "Prices", logger).append([])
    assert sheet.writes == []


def test_timestamp_is_written_in_the_agreed_format(logger):
    sheet = FakeSheet()
    HistoryTab(sheet, "Prices", logger).append([_reading()])
    assert sheet.frame.iloc[0]["timestamp"] == "2026-08-20 06:00:00"


def test_status_is_written_as_its_string_value(logger):
    sheet = FakeSheet()
    HistoryTab(sheet, "Prices", logger).append([_reading(status=PriceStatus.SUSPECT)])
    assert sheet.frame.iloc[0]["status"] == "suspect"


def test_null_price_is_written_blank(logger):
    sheet = FakeSheet()
    HistoryTab(sheet, "Prices", logger).append(
        [_reading(price=None, status=PriceStatus.NOT_FOUND)]
    )
    assert sheet.frame.iloc[0]["price"] == ""


def test_last_prices_uses_the_most_recent_non_null(logger):
    frame = _history(
        [
            {
                "timestamp": "2026-08-20 06:00:00",
                "item": "Widget",
                "website": "shop.example",
                "price": "100.0",
                "currency": "GBP",
                "status": "ok",
                "source_url": "",
                "note": "",
            },
            {
                "timestamp": "2026-08-20 12:00:00",
                "item": "Widget",
                "website": "shop.example",
                "price": "120.0",
                "currency": "GBP",
                "status": "ok",
                "source_url": "",
                "note": "",
            },
            {
                "timestamp": "2026-08-20 18:00:00",
                "item": "Widget",
                "website": "shop.example",
                "price": "",
                "currency": "GBP",
                "status": "not_found",
                "source_url": "",
                "note": "",
            },
        ]
    )
    tab = HistoryTab(FakeSheet(frame), "Prices", logger)
    assert tab.last_prices()[("Widget", "shop.example")] == 120.0


def test_last_prices_prefers_the_later_recorded_row_on_duplicate_timestamps(logger):
    frame = _history(
        [
            {
                "timestamp": "2026-08-20 06:00:00",
                "item": "Widget",
                "website": "shop.example",
                "price": "100.0",
                "currency": "GBP",
                "status": "ok",
                "source_url": "",
                "note": "",
            },
            {
                "timestamp": "2026-08-20 06:00:00",
                "item": "Widget",
                "website": "shop.example",
                "price": "200.0",
                "currency": "GBP",
                "status": "ok",
                "source_url": "",
                "note": "",
            },
        ]
    )
    tab = HistoryTab(FakeSheet(frame), "Prices", logger)
    assert tab.last_prices()[("Widget", "shop.example")] == 200.0


def test_known_items_includes_items_with_no_successful_price(logger):
    frame = _history(
        [
            {
                "timestamp": "2026-08-20 06:00:00",
                "item": "Ghost",
                "website": "shop.example",
                "price": "",
                "currency": "GBP",
                "status": "not_found",
                "source_url": "",
                "note": "",
            }
        ]
    )
    tab = HistoryTab(FakeSheet(frame), "Prices", logger)
    assert ("Ghost", "shop.example") in tab.known_items()


def test_summarise_computes_current_min_and_mean(logger):
    frame = _history(
        [
            {
                "timestamp": "2026-08-20 06:00:00",
                "item": "Widget",
                "website": "shop.example",
                "price": "100.0",
                "currency": "GBP",
                "status": "ok",
                "source_url": "",
                "note": "",
            },
            {
                "timestamp": "2026-08-20 12:00:00",
                "item": "Widget",
                "website": "shop.example",
                "price": "50.0",
                "currency": "GBP",
                "status": "ok",
                "source_url": "",
                "note": "",
            },
            {
                "timestamp": "2026-08-20 18:00:00",
                "item": "Widget",
                "website": "shop.example",
                "price": "60.0",
                "currency": "GBP",
                "status": "ok",
                "source_url": "",
                "note": "",
            },
        ]
    )
    row = HistoryTab(FakeSheet(frame), "Prices", logger).summarise().iloc[0]
    assert row["current"] == 60.0
    assert row["min"] == 50.0
    assert row["mean"] == 70.0
    assert row["last_checked"] == "2026-08-20 18:00:00"


def test_last_checked_reflects_failures_too(logger):
    frame = _history(
        [
            {
                "timestamp": "2026-08-20 06:00:00",
                "item": "Widget",
                "website": "shop.example",
                "price": "100.0",
                "currency": "GBP",
                "status": "ok",
                "source_url": "",
                "note": "",
            },
            {
                "timestamp": "2026-08-20 12:00:00",
                "item": "Widget",
                "website": "shop.example",
                "price": "",
                "currency": "GBP",
                "status": "error",
                "source_url": "",
                "note": "",
            },
        ]
    )
    row = HistoryTab(FakeSheet(frame), "Prices", logger).summarise().iloc[0]
    assert row["current"] == 100.0
    assert row["last_checked"] == "2026-08-20 12:00:00"


def test_item_with_no_prices_summarises_blank(logger):
    frame = _history(
        [
            {
                "timestamp": "2026-08-20 06:00:00",
                "item": "Ghost",
                "website": "shop.example",
                "price": "",
                "currency": "GBP",
                "status": "not_found",
                "source_url": "",
                "note": "",
            }
        ]
    )
    row = HistoryTab(FakeSheet(frame), "Prices", logger).summarise().iloc[0]
    assert row["current"] == ""
    assert row["min"] == ""
    assert row["mean"] == ""


def test_summarise_of_empty_history_is_empty(logger):
    assert HistoryTab(FakeSheet(), "Prices", logger).summarise().empty


def test_columns_end_with_status_so_trailing_cells_are_never_trimmed():
    assert COLUMNS[-1] == "status"


@pytest.mark.parametrize(
    "status, source_url",
    [
        # A real not_found/error reading never has a source_url: nothing was
        # found to link to. Only a successful OK reading would carry one.
        (PriceStatus.OK, "https://shop.example/w"),
        (PriceStatus.NOT_FOUND, ""),
        (PriceStatus.ERROR, ""),
    ],
)
def test_written_frames_final_column_is_never_empty_regardless_of_status(
    logger, status, source_url
):
    sheet = FakeSheet()
    HistoryTab(sheet, "Prices", logger).append(
        [_reading(price=None, status=status, source_url=source_url)]
    )
    last_column = sheet.frame.columns[-1]
    assert last_column == "status"
    assert sheet.frame.iloc[0][last_column] != ""


def test_round_trip_with_blank_source_url_and_note_keeps_frame_intact(logger):
    sheet = FakeSheet()
    tab = HistoryTab(sheet, "Prices", logger)
    # note is blank by PriceReading's own default; source_url is forced blank
    # here too, since that is the realistic shape of a not_found/error
    # reading — the exact case the column reorder exists to survive.
    tab.append([_reading(price=100.0, status=PriceStatus.OK, source_url="")])
    # Simulate the Sheets API omitting the trailing empty note/source_url cells
    # by re-reading through a frame with no NaN padding needed (full width).
    read_back = HistoryTab(FakeSheet(sheet.frame), "Prices", logger).read()
    assert list(read_back.columns) == COLUMNS
    assert read_back.iloc[0]["item"] == "Widget"
    assert read_back.iloc[0]["source_url"] == ""
    assert read_back.iloc[0]["note"] == ""
    assert read_back.iloc[0]["status"] == "ok"
    row = HistoryTab(FakeSheet(sheet.frame), "Prices", logger).summarise().iloc[0]
    assert row["current"] == 100.0


def test_read_fills_nan_with_empty_string(logger):
    # A manually-edited or ragged sheet can produce NaN in short columns;
    # read() must not let that NaN survive into the returned frame.
    frame = pd.DataFrame(
        [["2026-08-20 06:00:00", "Widget", "shop.example"]],
        columns=["timestamp", "item", "website"],
    )
    frame = frame.reindex(columns=COLUMNS)
    tab = HistoryTab(FakeSheet(frame), "Prices", logger)
    result = tab.read()
    assert result.iloc[0]["status"] == ""
    assert not result.isna().any().any()


def _checked_row(item: str, timestamp: str, status: str = "ok") -> dict:
    return {
        "timestamp": timestamp,
        "item": item,
        "website": "shop.example",
        "price": "100.00",
        "currency": "GBP",
        "source_url": "",
        "note": "",
        "status": status,
    }


def test_last_checked_is_empty_for_an_empty_history(logger):
    tab = HistoryTab(FakeSheet(), "Prices", logger)
    assert tab.last_checked() == {}


def test_last_checked_returns_the_newest_timestamp_per_item(logger):
    frame = _history(
        [
            _checked_row("Widget", "2026-08-20 06:00:00"),
            _checked_row("Widget", "2026-08-21 06:00:00"),
            _checked_row("Gadget", "2026-08-19 06:00:00"),
        ]
    )
    checked = HistoryTab(FakeSheet(frame), "Prices", logger).last_checked()
    assert checked[("Widget", "shop.example")] == datetime(2026, 8, 21, 6, 0, 0)
    assert checked[("Gadget", "shop.example")] == datetime(2026, 8, 19, 6, 0, 0)


def test_last_checked_ignores_row_order(logger):
    frame = _history(
        [
            _checked_row("Widget", "2026-08-21 06:00:00"),
            _checked_row("Widget", "2026-08-20 06:00:00"),
        ]
    )
    checked = HistoryTab(FakeSheet(frame), "Prices", logger).last_checked()
    assert checked[("Widget", "shop.example")] == datetime(2026, 8, 21, 6, 0, 0)


def test_last_checked_counts_failed_lookups(logger):
    """A not_found row spent an API call, so it counts as a check."""
    row = _checked_row("Widget", "2026-08-21 06:00:00", status="not_found")
    row["price"] = ""
    checked = HistoryTab(FakeSheet(_history([row])), "Prices", logger).last_checked()
    assert checked[("Widget", "shop.example")] == datetime(2026, 8, 21, 6, 0, 0)


def test_last_checked_drops_unparseable_timestamps(logger):
    """Leaving the item absent makes it look unchecked, so it gets refreshed —
    a hand-edited sheet must never silently suppress a lookup."""
    frame = _history([_checked_row("Widget", "not a timestamp")])
    assert HistoryTab(FakeSheet(frame), "Prices", logger).last_checked() == {}


def test_last_checked_keeps_other_items_when_one_row_is_unparseable(logger):
    frame = _history(
        [
            _checked_row("Widget", "broken"),
            _checked_row("Gadget", "2026-08-21 06:00:00"),
        ]
    )
    checked = HistoryTab(FakeSheet(frame), "Prices", logger).last_checked()
    assert ("Widget", "shop.example") not in checked
    assert checked[("Gadget", "shop.example")] == datetime(2026, 8, 21, 6, 0, 0)


def _priced(item: str, price: str, when: str, status: str = "ok") -> dict:
    return {
        "timestamp": when,
        "item": item,
        "website": "shop.example",
        "price": price,
        "currency": "GBP",
        "source_url": "",
        "note": "",
        "status": status,
    }


def test_price_stats_reports_the_mean_last_price_and_count(logger):
    frame = _history(
        [
            _priced("Widget", "100.0", "2026-08-20 06:00:00"),
            _priced("Widget", "50.0", "2026-08-20 12:00:00"),
            _priced("Widget", "60.0", "2026-08-20 18:00:00"),
        ]
    )
    stats = HistoryTab(FakeSheet(frame), "Prices", logger).price_stats()
    entry = stats[("Widget", "shop.example")]
    assert entry.mean == 70.0
    assert entry.last == 60.0
    assert entry.count == 3


def test_price_stats_ignores_rows_that_carry_no_price(logger):
    frame = _history(
        [
            _priced("Widget", "100.0", "2026-08-20 06:00:00"),
            _priced("Widget", "", "2026-08-20 12:00:00", status="not_found"),
            _priced("Widget", "60.0", "2026-08-20 18:00:00"),
        ]
    )
    entry = HistoryTab(FakeSheet(frame), "Prices", logger).price_stats()[
        ("Widget", "shop.example")
    ]
    assert entry.mean == 80.0
    assert entry.last == 60.0
    assert entry.count == 2


def test_price_stats_uses_timestamp_order_not_row_order(logger):
    frame = _history(
        [
            _priced("Widget", "60.0", "2026-08-20 18:00:00"),
            _priced("Widget", "100.0", "2026-08-20 06:00:00"),
        ]
    )
    entry = HistoryTab(FakeSheet(frame), "Prices", logger).price_stats()[
        ("Widget", "shop.example")
    ]
    assert entry.last == 60.0


def test_price_stats_of_an_empty_history_is_empty(logger):
    assert HistoryTab(FakeSheet(), "Prices", logger).price_stats() == {}
