"""Tests for the user-owned Control tab."""

import logging

import pandas as pd
import pytest

from price_monitor.sheets.control_tab import ControlTab


class FakeSheet:
    """Stands in for GoogleSheetInterface, recording writes and failures."""

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


def _control(rows: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"blocked": rows})


def test_blocked_sites_are_read_from_the_column(logger):
    sheet = FakeSheet(_control(["amazon.co.uk", "ebay.co.uk"]))
    assert ControlTab(sheet, "Control", logger).blocked() == {
        "amazon.co.uk",
        "ebay.co.uk",
    }


def test_blanks_and_whitespace_are_ignored(logger):
    sheet = FakeSheet(_control(["  argos.co.uk  ", "", "   "]))
    assert ControlTab(sheet, "Control", logger).blocked() == {"argos.co.uk"}


def test_entries_are_lowercased_so_case_does_not_matter(logger):
    sheet = FakeSheet(_control(["Amazon.CO.UK"]))
    assert ControlTab(sheet, "Control", logger).blocked() == {"amazon.co.uk"}


def test_an_empty_tab_blocks_nothing(logger):
    assert ControlTab(FakeSheet(), "Control", logger).blocked() == set()


def test_a_tab_without_the_column_blocks_nothing(logger):
    sheet = FakeSheet(pd.DataFrame({"something_else": ["x"]}))
    assert ControlTab(sheet, "Control", logger).blocked() == set()


def test_a_missing_tab_is_created_and_blocks_nothing(logger):
    """The API cannot parse a range for a tab that does not exist. That is the
    first-run case, and creating the tab makes the feature discoverable."""
    sheet = FakeSheet(error=RuntimeError("Unable to parse range: Control"))
    assert ControlTab(sheet, "Control", logger).blocked() == set()
    assert sheet.writes == ["Control"]
    assert list(sheet.frame.columns) == ["blocked"]


def test_a_malformed_tab_is_never_overwritten(logger, caplog):
    """A tab whose every row is short of its header makes pandas raise
    locally, before the API is involved. Writing then would clear the user's
    controls — destroying data in exactly the case they asked us to tolerate.
    """
    sheet = FakeSheet(error=ValueError("2 columns passed, passed data had 1 columns"))
    with caplog.at_level(logging.WARNING):
        assert ControlTab(sheet, "Control", logger).blocked() == set()

    assert sheet.writes == [], "must not write over a tab that still holds data"
    assert "could not be read" in "\n".join(r.message for r in caplog.records)


def test_a_malformed_tab_is_announced_not_just_logged(logger):
    """Silent degradation is the failure mode this project keeps hitting."""
    sent: list[str] = []
    sheet = FakeSheet(error=ValueError("ragged"))
    ControlTab(sheet, "Control", logger, notify=sent.append).blocked()
    assert any("Control" in message for message in sent)


def test_a_missing_tab_is_not_announced(logger):
    """A first run has no Control tab yet; that is normal, not a fault."""
    sent: list[str] = []
    sheet = FakeSheet(error=RuntimeError("Unable to parse range"))
    ControlTab(sheet, "Control", logger, notify=sent.append).blocked()
    assert sent == []


def test_a_failure_to_create_the_tab_does_not_stop_the_run(logger):
    """A sheet problem must never cost a sweep."""

    class Unwritable(FakeSheet):
        def write(self, sheet_name: str, df: pd.DataFrame) -> None:
            raise RuntimeError("no permission")

    sheet = Unwritable(error=RuntimeError("Unable to parse range"))
    assert ControlTab(sheet, "Control", logger).blocked() == set()
