"""Tests for the PriceMonitor controller: timing, orchestration, and the CLI parser."""

import logging

import pandas as pd
import pytest

from price_monitor.models import Item, PriceReading, PriceStatus
from price_monitor.price_monitor import PriceMonitor, args_parser


class FakeSheet:
    def __init__(self, tabs: dict[str, pd.DataFrame]):
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
    def __init__(self):
        self.priced: list[str] = []

    def price(self, item: Item, last_price, timestamp) -> PriceReading:
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
def logger():
    return logging.getLogger("test")


def _monitor(sheet, searcher, logger) -> PriceMonitor:
    return PriceMonitor.for_test(gsheet=sheet, searcher=searcher, logger=logger)


def _items(rows):
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
        def price(self, item, last_price, timestamp):
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
        def price(self, item, last_price, timestamp):
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


def test_parser_requires_secrets():
    with pytest.raises(SystemExit):
        args_parser().parse_args([])


def test_parser_accepts_once_flag():
    args = args_parser().parse_args(["--secrets", "/tmp/s", "--once"])
    assert args.once is True
    assert str(args.secrets) == "/tmp/s"
