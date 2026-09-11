"""Live checks against the real Gemini API and Google Sheets.

Run with: pytest -m integration
Requires PRICE_MONITOR_SECRETS to point at a configured secrets directory.
"""

import logging
from datetime import datetime

import pandas as pd
import pytest

from price_monitor.models import Item, PriceReading, PriceStatus
from price_monitor.search.market import MarketSearcher
from price_monitor.sheets.history_tab import COLUMNS, HistoryTab

pytestmark = pytest.mark.integration

# Never the configured real history tab — a name this unlikely to collide with
# a genuine sheet, created and torn down entirely within the test below.
_SCRATCH_HISTORY_TAB = "_price_monitor_integration_scratch_history"


def test_sheet_round_trip(config, logger, gsheet):
    from price_monitor.sheets.items_tab import ItemsTab

    items = ItemsTab(gsheet, config.price_ctrl.items_sheet, logger).read()
    assert isinstance(items, list)
    for item in items:
        assert item.name.strip()


def test_history_round_trip_survives_trailing_empty_cells(config, logger, gsheet):
    """Reproduces, against the real Sheets API, the defect that motivated
    putting `status` last in COLUMNS: the API omits trailing empty cells from
    each row, and a naive column layout would either pad missing cells with
    NaN or (if every row in the sheet is short) raise ValueError out of
    GoogleSheetInterface.read() before HistoryTab ever sees the data. A
    not_found reading has all nine detection cells and `note` blank — the
    realistic shape this defence has to survive — so this is the one
    assumption in the project that fakes cannot verify.

    Runs entirely against a dedicated scratch tab, deleted in a finally block
    regardless of outcome, so it cannot leave data behind or touch the
    configured Items/Prices tabs.
    """
    assert (
        _SCRATCH_HISTORY_TAB != config.price_ctrl.history_sheet
    ), "scratch tab name must never match the configured real history sheet"

    tab = HistoryTab(gsheet, _SCRATCH_HISTORY_TAB, logger)
    reading = PriceReading(
        timestamp=datetime(2026, 8, 20, 6, 0, 0),
        item="Integration Test Widget",
        currency="GBP",
        status=PriceStatus.NOT_FOUND,
        note="",
    )
    try:
        # Pre-create the tab with just a header row. HistoryTab.append() reads
        # before it writes, and GoogleSheetInterface.read() raises if the
        # named tab does not exist yet (the Sheets API errors on a range for
        # an unknown sheet) — that gap is orthogonal to the defect under test
        # here, so it is worked around rather than exercised.
        gsheet.write(sheet_name=_SCRATCH_HISTORY_TAB, df=pd.DataFrame(columns=COLUMNS))
        tab.append([reading])

        read_back = HistoryTab(gsheet, _SCRATCH_HISTORY_TAB, logger).read()

        assert list(read_back.columns) == COLUMNS
        assert len(read_back) == 1
        assert not read_back.isna().any().any()
        row = read_back.iloc[0]
        assert row["item"] == "Integration Test Widget"
        assert row["cheapest_price"] == ""
        assert row["cheapest_site"] == ""
        assert row["note"] == ""
        assert row["status"] == "not_found"
    finally:
        _delete_tab(gsheet, _SCRATCH_HISTORY_TAB, logger)


def test_live_sweep_returns_three_detections(config, logger):
    """One real sweep. Asserts shape and plausibility, never a specific price."""
    searcher = MarketSearcher(config.llm_config, config.price_ctrl, logger)
    item = Item(name="Raspberry Pi 5 8GB")

    reading = searcher.price_market(item, None, datetime.now())

    assert reading.status in {
        PriceStatus.OK,
        PriceStatus.SUSPECT,
        PriceStatus.NOT_FOUND,
    }
    if reading.cheapest is None:
        return
    assert 0 < reading.cheapest.price < config.price_ctrl.max_plausible_price
    assert reading.cheapest.site
    assert 0.0 <= reading.confidence <= 1.0
    if reading.best_value is not None and reading.best_rated is not None:
        assert reading.best_value.price >= reading.cheapest.price or (
            reading.cheapest.rating == ""
        )
    sources = int(reading.note.split()[0])
    assert sources >= 2, f"sweep found only {sources} retailer(s): {reading.note}"


def _delete_tab(gsheet, sheet_name: str, logger: logging.Logger) -> None:
    """Best-effort removal of a scratch tab.

    Reaches into GoogleSheetInterface's private Sheets client because no
    public delete-tab method exists. Never raises: a cleanup failure must not
    mask the test's real outcome, so it is logged and left for manual tidy-up.
    """
    try:
        file_id = gsheet._resolve_file_id(gsheet._remote_file)
        meta = (
            gsheet._sheets_service.spreadsheets()
            .get(spreadsheetId=file_id, fields="sheets.properties")
            .execute()
        )
        for sheet in meta.get("sheets", []):
            properties = sheet["properties"]
            if properties["title"] == sheet_name:
                gsheet._sheets_service.spreadsheets().batchUpdate(
                    spreadsheetId=file_id,
                    body={
                        "requests": [
                            {"deleteSheet": {"sheetId": properties["sheetId"]}}
                        ]
                    },
                ).execute()
                return
    except Exception as exc:  # best-effort cleanup only
        logger.warning(
            f"Could not delete scratch tab '{sheet_name}', remove it manually: {exc}"
        )
