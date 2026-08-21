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
    item="Widget", price=100.0, when="2026-08-20 06:00:00", status=PriceStatus.OK
):
    return PriceReading(
        timestamp=datetime.strptime(when, "%Y-%m-%d %H:%M:%S"),
        item=item,
        website="shop.example",
        price=price,
        currency="GBP",
        status=status,
        source_url="https://shop.example/w",
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
    "status", [PriceStatus.OK, PriceStatus.NOT_FOUND, PriceStatus.ERROR]
)
def test_written_frames_final_column_is_never_empty_regardless_of_status(
    logger, status
):
    sheet = FakeSheet()
    HistoryTab(sheet, "Prices", logger).append([_reading(price=None, status=status)])
    last_column = sheet.frame.columns[-1]
    assert last_column == "status"
    assert sheet.frame.iloc[0][last_column] != ""


def test_round_trip_with_blank_source_url_and_note_keeps_frame_intact(logger):
    sheet = FakeSheet()
    tab = HistoryTab(sheet, "Prices", logger)
    tab.append([_reading(price=100.0, status=PriceStatus.OK)])
    # Simulate the Sheets API omitting the trailing empty note/source_url cells
    # by re-reading through a frame with no NaN padding needed (full width).
    read_back = HistoryTab(FakeSheet(sheet.frame), "Prices", logger).read()
    assert list(read_back.columns) == COLUMNS
    assert read_back.iloc[0]["item"] == "Widget"
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
