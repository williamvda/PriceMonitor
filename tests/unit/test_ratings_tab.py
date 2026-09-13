"""Tests for the cached Ratings tab."""

import logging
from datetime import datetime, timedelta

import pandas as pd
import pytest

from price_monitor.models import Rating
from price_monitor.sheets.ratings_tab import COLUMNS, RatingsTab

TODAY = datetime.now().strftime("%Y-%m-%d")


class FakeSheet:
    def __init__(
        self, frame: pd.DataFrame | None = None, error: Exception | None = None
    ):
        self.frame = frame if frame is not None else pd.DataFrame()
        self.error = error
        self.writes: list[str] = []

    def read(self, sheet_name: str) -> pd.DataFrame:
        if self.error is not None:
            raise self.error
        return self.frame.copy()

    def write(self, sheet_name: str, df: pd.DataFrame) -> None:
        self.writes.append(sheet_name)
        self.frame = df.copy()
        self.error = None


@pytest.fixture
def logger():
    return logging.getLogger("test")


def _rows(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLUMNS)


def _row(site: str, rating: str = "4.1", checked: str = TODAY) -> dict:
    return {
        "site": site,
        "rating": rating,
        "reviews": "900",
        "source": "trustpilot",
        "checked": checked,
    }


def test_columns_end_with_checked_so_trailing_cells_are_never_trimmed():
    assert COLUMNS[-1] == "checked"


def test_ratings_are_read_by_site(logger):
    tab = RatingsTab(FakeSheet(_rows([_row("argos.co.uk")])), "Ratings", logger)
    ratings = tab.read()
    assert ratings["argos.co.uk"].rating == 4.1
    assert ratings["argos.co.uk"].reviews == 900
    assert ratings["argos.co.uk"].source == "trustpilot"


def test_a_checked_site_with_no_rating_reads_as_a_known_blank(logger):
    """'We asked and there was nothing' must be distinguishable from
    'we have not asked', or it would be looked up forever."""
    frame = _rows(
        [
            {
                "site": "tiny.co.uk",
                "rating": "",
                "reviews": "",
                "source": "",
                "checked": TODAY,
            }
        ]
    )
    ratings = RatingsTab(FakeSheet(frame), "Ratings", logger).read()
    assert "tiny.co.uk" in ratings
    assert ratings["tiny.co.uk"].rating is None


def test_an_empty_tab_reads_as_no_ratings(logger):
    assert RatingsTab(FakeSheet(), "Ratings", logger).read() == {}


def test_a_hand_typed_out_of_range_rating_is_stored_as_none(logger):
    """A retailer's rating enters from two places — the automated lookup and
    a human correcting it by hand — and neither can vouch for the other, so
    the same five-point guard applies to a hand-typed value too."""
    tab = RatingsTab(
        FakeSheet(_rows([_row("argos.co.uk", rating="45")])), "Ratings", logger
    )
    assert tab.read()["argos.co.uk"].rating is None


def test_an_unreadable_tab_returns_none_and_is_never_written(logger):
    """write() clears before it writes, so a tab we cannot read must not be
    touched — it may still hold every rating we have."""
    sheet = FakeSheet(error=ValueError("2 columns passed, passed data had 1 columns"))
    tab = RatingsTab(sheet, "Ratings", logger)
    assert tab.read() is None
    tab.register({"argos.co.uk"})
    tab.store({"argos.co.uk": Rating(4.1, 900, "trustpilot")})
    assert sheet.writes == []


def test_a_missing_tab_is_created_empty(logger):
    sheet = FakeSheet(error=RuntimeError("Unable to parse range: Ratings"))
    assert RatingsTab(sheet, "Ratings", logger).read() == {}
    assert sheet.writes == ["Ratings"]
    assert list(sheet.frame.columns) == COLUMNS


def test_stale_includes_sites_never_checked(logger):
    frame = _rows(
        [
            {
                "site": "new.co.uk",
                "rating": "",
                "reviews": "",
                "source": "",
                "checked": "",
            }
        ]
    )
    assert RatingsTab(FakeSheet(frame), "Ratings", logger).stale(30) == ["new.co.uk"]


def test_stale_includes_sites_checked_too_long_ago(logger):
    old = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    frame = _rows([_row("argos.co.uk", checked=old)])
    assert RatingsTab(FakeSheet(frame), "Ratings", logger).stale(30) == ["argos.co.uk"]


def test_stale_excludes_a_recently_checked_site(logger):
    frame = _rows([_row("argos.co.uk", checked=TODAY)])
    assert RatingsTab(FakeSheet(frame), "Ratings", logger).stale(30) == []


def test_an_unparseable_date_counts_as_never_checked(logger):
    """A hand-edited cell must make a site stale, never permanently fresh."""
    frame = _rows([_row("argos.co.uk", checked="last tuesday")])
    assert RatingsTab(FakeSheet(frame), "Ratings", logger).stale(30) == ["argos.co.uk"]


def test_a_future_checked_date_counts_as_never_checked(logger):
    """A date we could not have written is a hand-edit error, and the safe
    direction for an error is always 'look it up again', never 'never look
    again' — otherwise a typo'd year freezes that retailer's rating for
    decades."""
    future = (datetime.now() + timedelta(days=5 * 365)).strftime("%Y-%m-%d")
    frame = _rows([_row("argos.co.uk", checked=future)])
    assert RatingsTab(FakeSheet(frame), "Ratings", logger).stale(30) == ["argos.co.uk"]


def test_a_future_checked_date_is_grouped_with_never_checked_sites(logger):
    old = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    future = (datetime.now() + timedelta(days=5 * 365)).strftime("%Y-%m-%d")
    frame = _rows(
        [
            _row("old.co.uk", checked=old),
            _row("future.co.uk", checked=future),
        ]
    )
    assert RatingsTab(FakeSheet(frame), "Ratings", logger).stale(30) == [
        "future.co.uk",
        "old.co.uk",
    ]


def test_never_checked_sites_come_before_merely_stale_ones(logger):
    """A retailer we have never rated is worth more than refreshing one we
    already have a usable number for."""
    old = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    frame = _rows(
        [
            _row("old.co.uk", checked=old),
            {
                "site": "new.co.uk",
                "rating": "",
                "reviews": "",
                "source": "",
                "checked": "",
            },
        ]
    )
    assert RatingsTab(FakeSheet(frame), "Ratings", logger).stale(30) == [
        "new.co.uk",
        "old.co.uk",
    ]


def test_register_appends_only_unseen_sites(logger):
    sheet = FakeSheet(_rows([_row("argos.co.uk")]))
    RatingsTab(sheet, "Ratings", logger).register({"argos.co.uk", "new.co.uk"})
    assert set(sheet.frame["site"]) == {"argos.co.uk", "new.co.uk"}
    assert sheet.frame[sheet.frame["site"] == "new.co.uk"].iloc[0]["checked"] == ""


def test_register_writes_nothing_when_every_site_is_known(logger):
    sheet = FakeSheet(_rows([_row("argos.co.uk")]))
    RatingsTab(sheet, "Ratings", logger).register({"argos.co.uk"})
    assert sheet.writes == []


def test_store_stamps_checked_even_for_a_site_with_no_rating(logger):
    """Otherwise a retailer with no Trustpilot presence is looked up forever."""
    sheet = FakeSheet(
        _rows(
            [
                {
                    "site": "tiny.co.uk",
                    "rating": "",
                    "reviews": "",
                    "source": "",
                    "checked": "",
                }
            ]
        )
    )
    RatingsTab(sheet, "Ratings", logger).store({"tiny.co.uk": Rating()})
    row = sheet.frame.iloc[0]
    assert row["checked"] == TODAY
    assert row["rating"] == ""


def test_a_comma_formatted_review_count_is_not_lost(logger):
    """Review counts gate MIN_RATING_COUNT, so a real count silently going
    blank can flip a rating from trusted to untrusted."""
    frame = _rows([_row("argos.co.uk", rating="4.1")])
    frame.loc[0, "reviews"] = "1,200"
    ratings = RatingsTab(FakeSheet(frame), "Ratings", logger).read()
    assert ratings["argos.co.uk"].reviews == 1200


def test_store_updates_an_existing_row_rather_than_duplicating_it(logger):
    sheet = FakeSheet(_rows([_row("argos.co.uk", rating="3.0")]))
    RatingsTab(sheet, "Ratings", logger).store(
        {"argos.co.uk": Rating(4.4, 1200, "trustpilot")}
    )
    assert len(sheet.frame) == 1
    assert sheet.frame.iloc[0]["rating"] == "4.4"
