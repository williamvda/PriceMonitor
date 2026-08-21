"""Tests for items tab reader and summary writer."""

import logging

import pandas as pd
import pytest

from price_monitor.models import Item
from price_monitor.sheets.items_tab import ItemsTab


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
    return pd.DataFrame(rows, columns=["item", "website"])


def test_reads_items_in_sheet_order(logger):
    frame = _items_frame(
        [
            {"item": "Widget", "website": "shop.example"},
            {"item": "Gadget", "website": "other.example"},
        ]
    )
    items = ItemsTab(FakeSheet(frame), "Items", logger).read()
    assert items == [
        Item(name="Widget", website="shop.example"),
        Item(name="Gadget", website="other.example"),
    ]


def test_blank_name_row_is_skipped(logger):
    frame = _items_frame(
        [
            {"item": "", "website": "shop.example"},
            {"item": "Widget", "website": "shop.example"},
        ]
    )
    assert ItemsTab(FakeSheet(frame), "Items", logger).read() == [
        Item(name="Widget", website="shop.example")
    ]


def test_blank_website_row_is_skipped(logger):
    frame = _items_frame([{"item": "Widget", "website": "  "}])
    assert ItemsTab(FakeSheet(frame), "Items", logger).read() == []


def test_surrounding_whitespace_is_trimmed(logger):
    frame = _items_frame([{"item": "  Widget  ", "website": " shop.example "}])
    assert ItemsTab(FakeSheet(frame), "Items", logger).read() == [
        Item(name="Widget", website="shop.example")
    ]


def test_duplicate_pairs_are_deduplicated(logger):
    frame = _items_frame(
        [
            {"item": "Widget", "website": "shop.example"},
            {"item": "Widget", "website": "shop.example"},
        ]
    )
    assert len(ItemsTab(FakeSheet(frame), "Items", logger).read()) == 1


def test_same_item_on_two_sites_is_kept(logger):
    frame = _items_frame(
        [
            {"item": "Widget", "website": "shop.example"},
            {"item": "Widget", "website": "other.example"},
        ]
    )
    assert len(ItemsTab(FakeSheet(frame), "Items", logger).read()) == 2


def test_empty_tab_yields_no_items(logger):
    assert ItemsTab(FakeSheet(), "Items", logger).read() == []


def test_tab_without_expected_headers_yields_no_items(logger):
    frame = pd.DataFrame([{"thing": "Widget"}])
    assert ItemsTab(FakeSheet(frame), "Items", logger).read() == []


def test_write_summary_uses_write_so_deletions_do_not_linger(logger):
    sheet = FakeSheet()
    summary = pd.DataFrame(
        [
            {
                "item": "Widget",
                "website": "shop.example",
                "current": 60.0,
                "min": 50.0,
                "mean": 70.0,
                "last_checked": "2026-08-20 18:00:00",
            }
        ]
    )
    ItemsTab(sheet, "Items", logger).write_summary(
        [Item(name="Widget", website="shop.example")], summary
    )
    assert sheet.writes == ["write"]
    row = sheet.frame.iloc[0]
    assert row["current"] == 60.0
    assert row["mean"] == 70.0


def test_item_without_history_gets_blank_summary_cells(logger):
    sheet = FakeSheet()
    ItemsTab(sheet, "Items", logger).write_summary(
        [Item(name="New", website="shop.example")], pd.DataFrame()
    )
    row = sheet.frame.iloc[0]
    assert row["item"] == "New"
    assert row["current"] == ""
    assert row["last_checked"] == ""


def test_summary_row_order_follows_the_items_list(logger):
    sheet = FakeSheet()
    items = [
        Item(name="B", website="shop.example"),
        Item(name="A", website="shop.example"),
    ]
    ItemsTab(sheet, "Items", logger).write_summary(items, pd.DataFrame())
    assert list(sheet.frame["item"]) == ["B", "A"]
