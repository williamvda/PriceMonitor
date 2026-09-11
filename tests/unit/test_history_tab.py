"""Tests for the append-only price history tab."""

import logging
from datetime import datetime

import pandas as pd
import pytest

from price_monitor.models import Detection, PriceReading, PriceStatus
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
    site="shop.example",
    rating="",
    rated=None,
    value=None,
    confidence=None,
    sources=None,
    note="",
):
    """A sweep whose three detections default to the same cheapest offer."""
    cheapest = None if price is None else Detection(price, site, rating)
    return PriceReading(
        timestamp=datetime.strptime(when, "%Y-%m-%d %H:%M:%S"),
        item=item,
        currency="GBP",
        status=status,
        best_rated=cheapest if rated is None else rated,
        cheapest=cheapest,
        best_value=cheapest if value is None else value,
        confidence=confidence,
        sources=sources,
        note=note,
    )


def _row(
    item: str = "Widget",
    when: str = "2026-08-20 06:00:00",
    price: str = "100.00",
    status: str = "ok",
    site: str = "shop.example",
    confidence: str = "",
    sources: str = "",
) -> dict:
    """One recorded sweep, in the string form the sheet hands back."""
    return {
        "timestamp": when,
        "item": item,
        "rated_price": price,
        "rated_site": site,
        "rated_rating": "",
        "cheapest_price": price,
        "cheapest_site": site,
        "cheapest_rating": "",
        "value_price": price,
        "value_site": site,
        "value_rating": "",
        "confidence": confidence,
        "sources": sources,
        "note": "",
        "status": status,
    }


def _history(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLUMNS)


def test_first_append_uses_write_so_the_tab_is_created(logger):
    sheet = FakeSheet()
    HistoryTab(sheet, "Prices", logger).append([_reading()])
    assert sheet.writes == ["write"]
    assert len(sheet.frame) == 1


def test_subsequent_append_uses_update_to_protect_history(logger):
    sheet = FakeSheet(_history([_row()]))
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


def test_all_three_detections_round_trip_into_their_own_cells(logger):
    sheet = FakeSheet()
    tab = HistoryTab(sheet, "Prices", logger)
    tab.append(
        [
            _reading(
                price=100.0,
                site="cheap.example",
                rated=Detection(140.0, "john-lewis.com", "4.6 Trustpilot"),
                value=Detection(110.0, "argos.co.uk", "4.4 Trustpilot"),
            )
        ]
    )
    row = tab.read().iloc[0]
    assert row["rated_price"] == "140.00"
    assert row["rated_site"] == "john-lewis.com"
    assert row["rated_rating"] == "4.6 Trustpilot"
    assert row["cheapest_price"] == "100.00"
    assert row["cheapest_site"] == "cheap.example"
    assert row["cheapest_rating"] == ""
    assert row["value_price"] == "110.00"
    assert row["value_site"] == "argos.co.uk"
    assert row["value_rating"] == "4.4 Trustpilot"


def test_a_failed_sweep_writes_every_detection_cell_blank(logger):
    sheet = FakeSheet()
    HistoryTab(sheet, "Prices", logger).append(
        [_reading(price=None, status=PriceStatus.NOT_FOUND)]
    )
    row = sheet.frame.iloc[0]
    for column in COLUMNS:
        if column not in ("timestamp", "item", "status"):
            assert row[column] == "", column


def test_a_sweep_with_no_rated_retailer_blanks_only_those_cells(logger):
    """`best_rated` is absent when nothing was rated; the cheapest still stands."""
    sheet = FakeSheet()
    HistoryTab(sheet, "Prices", logger).append(
        [
            PriceReading(
                timestamp=datetime(2026, 9, 11, 6, 0, 0),
                item="Widget",
                currency="GBP",
                status=PriceStatus.OK,
                cheapest=Detection(100.0, "shop.example"),
            )
        ]
    )
    row = sheet.frame.iloc[0]
    assert row["rated_price"] == ""
    assert row["value_price"] == ""
    assert row["cheapest_price"] == "100.00"


def test_last_prices_uses_the_most_recent_non_null_cheapest(logger):
    frame = _history(
        [
            _row(price="100.0"),
            _row(price="120.0", when="2026-08-20 12:00:00"),
            _row(price="", when="2026-08-20 18:00:00", status="not_found"),
        ]
    )
    tab = HistoryTab(FakeSheet(frame), "Prices", logger)
    assert tab.last_prices()["Widget"] == 120.0


def test_last_prices_prefers_the_later_recorded_row_on_duplicate_timestamps(logger):
    frame = _history([_row(price="100.0"), _row(price="200.0")])
    tab = HistoryTab(FakeSheet(frame), "Prices", logger)
    assert tab.last_prices()["Widget"] == 200.0


def test_known_items_includes_items_with_no_successful_price(logger):
    frame = _history([_row(item="Ghost", price="", status="not_found")])
    tab = HistoryTab(FakeSheet(frame), "Prices", logger)
    assert "Ghost" in tab.known_items()


def test_summarise_computes_min_and_mean_over_the_cheapest_series(logger):
    frame = _history(
        [
            _row(price="100.0"),
            _row(price="50.0", when="2026-08-20 12:00:00"),
            _row(price="60.0", when="2026-08-20 18:00:00"),
        ]
    )
    row = HistoryTab(FakeSheet(frame), "Prices", logger).summarise().iloc[0]
    assert row["cheapest_price"] == 60.0
    assert row["min"] == 50.0
    assert row["mean"] == 70.0
    assert row["last_checked"] == "2026-08-20 18:00:00"


def test_summarise_reports_the_latest_rows_nine_detection_cells(logger):
    sheet = FakeSheet()
    tab = HistoryTab(sheet, "Prices", logger)
    tab.append(
        [
            _reading(price=120.0, site="old.example", when="2026-09-11 06:00:00"),
            _reading(
                price=100.0,
                site="cheap.example",
                when="2026-09-11 12:00:00",
                rated=Detection(140.0, "john-lewis.com", "4.6 Trustpilot"),
                value=Detection(110.0, "argos.co.uk", "4.4 Trustpilot"),
                confidence=0.9,
                sources=4,
            ),
        ]
    )
    row = tab.summarise().iloc[0]
    assert row["rated_price"] == 140.0
    assert row["rated_site"] == "john-lewis.com"
    assert row["rated_rating"] == "4.6 Trustpilot"
    assert row["cheapest_price"] == 100.0
    assert row["cheapest_site"] == "cheap.example"
    assert row["value_price"] == 110.0
    assert row["value_site"] == "argos.co.uk"
    assert row["value_rating"] == "4.4 Trustpilot"
    assert row["confidence"] == "0.90"
    assert row["sources"] == "4"


def test_a_failed_sweep_contributes_nothing_but_a_moving_timestamp(logger):
    """A run of failures must not look like a healthy stale price."""
    frame = _history(
        [
            _row(price="100.0"),
            _row(price="", when="2026-08-20 12:00:00", status="error"),
        ]
    )
    row = HistoryTab(FakeSheet(frame), "Prices", logger).summarise().iloc[0]
    assert row["cheapest_price"] == 100.0
    assert row["mean"] == 100.0
    assert row["last_checked"] == "2026-08-20 12:00:00"


def test_item_with_no_prices_summarises_blank(logger):
    frame = _history([_row(item="Ghost", price="", status="not_found")])
    row = HistoryTab(FakeSheet(frame), "Prices", logger).summarise().iloc[0]
    assert row["cheapest_price"] == ""
    assert row["rated_price"] == ""
    assert row["min"] == ""
    assert row["mean"] == ""
    assert row["confidence"] == ""
    assert row["sources"] == ""


def test_summarise_of_empty_history_is_empty(logger):
    assert HistoryTab(FakeSheet(), "Prices", logger).summarise().empty


def test_columns_end_with_status_so_trailing_cells_are_never_trimmed():
    assert COLUMNS[-1] == "status"


@pytest.mark.parametrize(
    "status",
    [PriceStatus.OK, PriceStatus.NOT_FOUND, PriceStatus.ERROR],
)
def test_written_frames_final_column_is_never_empty_regardless_of_status(
    logger, status
):
    sheet = FakeSheet()
    HistoryTab(sheet, "Prices", logger).append([_reading(price=None, status=status)])
    last_column = sheet.frame.columns[-1]
    assert last_column == "status"
    assert sheet.frame.iloc[0][last_column] != ""


def test_round_trip_of_a_fully_blank_sweep_keeps_the_frame_intact(logger):
    """The realistic shape of a not_found sweep — every cell but status blank —
    is the exact case the column order exists to survive."""
    sheet = FakeSheet()
    HistoryTab(sheet, "Prices", logger).append(
        [_reading(price=None, status=PriceStatus.NOT_FOUND)]
    )
    read_back = HistoryTab(FakeSheet(sheet.frame), "Prices", logger).read()
    assert list(read_back.columns) == COLUMNS
    assert read_back.iloc[0]["item"] == "Widget"
    assert read_back.iloc[0]["note"] == ""
    assert read_back.iloc[0]["status"] == "not_found"


def test_read_fills_nan_with_empty_string(logger):
    # A manually-edited or ragged sheet can produce NaN in short columns;
    # read() must not let that NaN survive into the returned frame.
    frame = pd.DataFrame(
        [["2026-08-20 06:00:00", "Widget"]], columns=["timestamp", "item"]
    )
    frame = frame.reindex(columns=COLUMNS)
    result = HistoryTab(FakeSheet(frame), "Prices", logger).read()
    assert result.iloc[0]["status"] == ""
    assert not result.isna().any().any()


def test_last_checked_is_empty_for_an_empty_history(logger):
    tab = HistoryTab(FakeSheet(), "Prices", logger)
    assert tab.last_checked() == {}


def test_last_checked_returns_the_newest_timestamp_per_item(logger):
    frame = _history(
        [
            _row("Widget", "2026-08-20 06:00:00"),
            _row("Widget", "2026-08-21 06:00:00"),
            _row("Gadget", "2026-08-19 06:00:00"),
        ]
    )
    checked = HistoryTab(FakeSheet(frame), "Prices", logger).last_checked()
    assert checked["Widget"] == datetime(2026, 8, 21, 6, 0, 0)
    assert checked["Gadget"] == datetime(2026, 8, 19, 6, 0, 0)


def test_last_checked_ignores_row_order(logger):
    frame = _history(
        [
            _row("Widget", "2026-08-21 06:00:00"),
            _row("Widget", "2026-08-20 06:00:00"),
        ]
    )
    checked = HistoryTab(FakeSheet(frame), "Prices", logger).last_checked()
    assert checked["Widget"] == datetime(2026, 8, 21, 6, 0, 0)


def test_last_checked_counts_failed_sweeps(logger):
    """A not_found row spent an API call, so it counts as a check."""
    frame = _history(
        [_row("Widget", "2026-08-21 06:00:00", price="", status="not_found")]
    )
    checked = HistoryTab(FakeSheet(frame), "Prices", logger).last_checked()
    assert checked["Widget"] == datetime(2026, 8, 21, 6, 0, 0)


def test_last_checked_drops_unparseable_timestamps(logger):
    """Leaving the item absent makes it look unchecked, so it gets refreshed —
    a hand-edited sheet must never silently suppress a lookup."""
    frame = _history([_row("Widget", "not a timestamp")])
    assert HistoryTab(FakeSheet(frame), "Prices", logger).last_checked() == {}


def test_last_checked_keeps_other_items_when_one_row_is_unparseable(logger):
    frame = _history([_row("Widget", "broken"), _row("Gadget", "2026-08-21 06:00:00")])
    checked = HistoryTab(FakeSheet(frame), "Prices", logger).last_checked()
    assert "Widget" not in checked
    assert checked["Gadget"] == datetime(2026, 8, 21, 6, 0, 0)


def test_price_stats_reports_the_mean_last_price_and_count(logger):
    frame = _history(
        [
            _row(price="100.0"),
            _row(price="50.0", when="2026-08-20 12:00:00"),
            _row(price="60.0", when="2026-08-20 18:00:00"),
        ]
    )
    entry = HistoryTab(FakeSheet(frame), "Prices", logger).price_stats()["Widget"]
    assert entry.mean == 70.0
    assert entry.last == 60.0
    assert entry.count == 3


def test_price_stats_ignores_rows_that_carry_no_cheapest_price(logger):
    frame = _history(
        [
            _row(price="100.0"),
            _row(price="", when="2026-08-20 12:00:00", status="not_found"),
            _row(price="60.0", when="2026-08-20 18:00:00"),
        ]
    )
    entry = HistoryTab(FakeSheet(frame), "Prices", logger).price_stats()["Widget"]
    assert entry.mean == 80.0
    assert entry.last == 60.0
    assert entry.count == 2


def test_price_stats_uses_timestamp_order_not_row_order(logger):
    frame = _history(
        [
            _row(price="60.0", when="2026-08-20 18:00:00"),
            _row(price="100.0", when="2026-08-20 06:00:00"),
        ]
    )
    entry = HistoryTab(FakeSheet(frame), "Prices", logger).price_stats()["Widget"]
    assert entry.last == 60.0


def test_price_stats_of_an_empty_history_is_empty(logger):
    assert HistoryTab(FakeSheet(), "Prices", logger).price_stats() == {}


def test_confidence_renders_with_two_decimal_places(logger):
    """The spec documents this column as 0.00-1.00, so a perfect score must
    render as "1.00" and 0.90 must not collapse to "0.9" — both would
    otherwise misalign the column against the rest of its values."""
    sheet = FakeSheet()
    tab = HistoryTab(sheet, "Prices", logger)
    tab.append(
        [
            _reading(confidence=1.0),
            _reading(confidence=0.9),
            _reading(confidence=0.0),
        ]
    )
    assert list(tab.read()["confidence"]) == ["1.00", "0.90", "0.00"]


def test_source_count_round_trips_and_status_stays_last(logger):
    sheet = FakeSheet()
    history = HistoryTab(sheet, "Prices", logger)
    history.append([_reading(sources=4, confidence=0.95)])

    frame = history.read()
    assert list(frame.columns)[-1] == "status"
    assert frame["sources"].iloc[0] == "4"


def test_a_failed_sweep_records_no_source_count(logger):
    sheet = FakeSheet()
    history = HistoryTab(sheet, "Prices", logger)
    history.append([_reading(price=None, status=PriceStatus.ERROR)])
    assert history.read()["sources"].iloc[0] == ""
