"""Tests for items tab reader and summary writer."""

import logging

import pandas as pd
import pytest

from price_monitor.models import Item
from price_monitor.sheets.items_tab import COLUMNS, ItemsTab


class FakeSheet:
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


def _items_frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["item"])


def _summary(**overrides) -> pd.DataFrame:
    row = {
        "item": "Widget",
        "rated_price": 140.0,
        "rated_site": "john-lewis.com",
        "rated_rating": "4.6 Trustpilot",
        "cheapest_price": 100.0,
        "cheapest_site": "cheap.example",
        "cheapest_rating": "",
        "value_price": 110.0,
        "value_site": "argos.co.uk",
        "value_rating": "4.4 Trustpilot",
        "min": 90.0,
        "mean": 105.0,
        "confidence": "0.90",
        "sources": "4",
        "last_checked": "2026-09-11 06:00:00",
    }
    row.update(overrides)
    return pd.DataFrame([row])


def test_reads_items_in_sheet_order(logger):
    frame = _items_frame([{"item": "Widget"}, {"item": "Gadget"}])
    items = ItemsTab(FakeSheet(frame), "Items", logger).read()
    assert items == [Item(name="Widget"), Item(name="Gadget")]


def test_blank_name_row_is_skipped(logger):
    frame = _items_frame([{"item": ""}, {"item": "Widget"}])
    assert ItemsTab(FakeSheet(frame), "Items", logger).read() == [Item(name="Widget")]


def test_surrounding_whitespace_is_trimmed(logger):
    frame = _items_frame([{"item": "  Widget  "}])
    assert ItemsTab(FakeSheet(frame), "Items", logger).read() == [Item(name="Widget")]


def test_duplicate_names_are_deduplicated(logger, caplog):
    """The key is the name alone now, so a repeated name is one item, not two
    — otherwise the same sweep would be billed and recorded twice."""
    frame = _items_frame([{"item": "Widget"}, {"item": "Widget"}])
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        items = ItemsTab(FakeSheet(frame), "Items", logger).read()
    assert items == [Item(name="Widget")]
    assert any("duplicate 'Widget'" in record.message for record in caplog.records)


def test_empty_tab_yields_no_items(logger):
    assert ItemsTab(FakeSheet(), "Items", logger).read() == []


def test_tab_without_an_item_header_yields_no_items(logger):
    frame = pd.DataFrame([{"thing": "Widget"}])
    assert ItemsTab(FakeSheet(frame), "Items", logger).read() == []


def test_none_item_is_skipped_with_warning(logger, caplog):
    frame = _items_frame([{"item": None}, {"item": "Widget"}])
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        items = ItemsTab(FakeSheet(frame), "Items", logger).read()
    assert items == [Item(name="Widget")]
    assert any("blank item" in record.message for record in caplog.records)


def test_nan_item_is_skipped_with_warning(logger, caplog):
    frame = _items_frame([{"item": float("nan")}, {"item": "Widget"}])
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        items = ItemsTab(FakeSheet(frame), "Items", logger).read()
    assert items == [Item(name="Widget")]
    assert any("blank item" in record.message for record in caplog.records)


def test_ragged_frame_from_api_does_not_create_nan_items(logger, caplog):
    # Simulates what GoogleSheetInterface.read() does: builds a DataFrame from
    # ragged rows where trailing cells are omitted, padding with NaN.
    frame = pd.DataFrame([["Widget", "x"], [None, None]], columns=["item", "min"])
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        items = ItemsTab(FakeSheet(frame), "Items", logger).read()
    assert items == [Item(name="Widget")]
    assert any("blank item" in record.message for record in caplog.records)


def test_a_stale_three_column_sheet_still_reads(logger):
    """An existing sheet has item/website/market; only `item` matters now."""
    frame = pd.DataFrame(
        [
            {"item": "Widget", "website": "shop.example", "market": "yes"},
            {"item": "Gadget", "website": "other.example", "market": ""},
        ]
    )
    items = ItemsTab(FakeSheet(frame), "Items", logger).read()
    assert [i.name for i in items] == ["Widget", "Gadget"]


def test_write_summary_uses_write_so_deletions_do_not_linger(logger):
    sheet = FakeSheet()
    ItemsTab(sheet, "Items", logger).write_summary([Item(name="Widget")], _summary())
    assert sheet.writes == ["write"]


def test_write_summary_places_all_three_detections_on_the_item_row(logger):
    sheet = FakeSheet()
    ItemsTab(sheet, "Items", logger).write_summary([Item(name="Widget")], _summary())

    row = sheet.frame.iloc[0]
    assert list(sheet.frame.columns) == COLUMNS
    assert row["rated_price"] == 140.0
    assert row["rated_site"] == "john-lewis.com"
    assert row["rated_rating"] == "4.6 Trustpilot"
    assert row["cheapest_price"] == 100.0
    assert row["value_site"] == "argos.co.uk"
    assert row["min"] == 90.0
    assert row["mean"] == 105.0
    assert row["confidence"] == "0.90"
    assert row["sources"] == "4"
    assert row["last_checked"] == "2026-09-11 06:00:00"


def test_an_unswept_item_still_anchors_the_last_column(logger):
    """Every other column is blank for an item never swept, so only
    `last_checked` keeps the row from being trimmed away on the next read."""
    sheet = FakeSheet()
    ItemsTab(sheet, "Items", logger).write_summary(
        [Item(name="Widget")], pd.DataFrame()
    )

    row = sheet.frame.iloc[0]
    assert list(sheet.frame.columns)[-1] == "last_checked"
    for column in COLUMNS[1:-1]:
        assert row[column] == "", column
    assert row["last_checked"] == "never"


def test_an_item_missing_from_the_summary_is_still_written(logger):
    """A new item added mid-run has no history row yet; it must still appear."""
    sheet = FakeSheet()
    ItemsTab(sheet, "Items", logger).write_summary(
        [Item(name="Widget"), Item(name="New")], _summary()
    )
    assert list(sheet.frame["item"]) == ["Widget", "New"]
    assert sheet.frame.iloc[1]["last_checked"] == "never"
    assert sheet.frame.iloc[1]["cheapest_price"] == ""


def test_summary_row_order_follows_the_items_list(logger):
    sheet = FakeSheet()
    ItemsTab(sheet, "Items", logger).write_summary(
        [Item(name="B"), Item(name="A")], pd.DataFrame()
    )
    assert list(sheet.frame["item"]) == ["B", "A"]
