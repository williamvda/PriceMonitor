"""Live checks against the real Gemini API and Google Sheets.

Run with: pytest -m integration
Requires PRICE_MONITOR_SECRETS to point at a configured secrets directory.
"""

import logging
from dataclasses import replace
from datetime import datetime

import pandas as pd
import pytest

from price_monitor.models import Item, PriceReading, PriceStatus
from price_monitor.search.searcher import PriceSearcher
from price_monitor.sheets.history_tab import COLUMNS, HistoryTab

pytestmark = pytest.mark.integration

# Never the configured real history tab — a name this unlikely to collide with
# a genuine sheet, created and torn down entirely within the test below.
_SCRATCH_HISTORY_TAB = "_price_monitor_integration_scratch_history"

# Chosen because search grounding can actually reach it: this retailer's prices
# are in Google's index, and a bare domain routes to the site-search prompt
# rather than a page fetch. Bot-gated targets are useless as a canary here —
# amazon.co.uk, and printerland's own product pages (Cloudflare serves a
# challenge to any automated fetch), return not_found however well the pipeline
# works. Before "fixing" a failure by swapping this item, check the product is
# still listed: a genuine regression looks the same as a delisted SKU.
_LIVE_ITEM = Item(name="Epson EcoTank ET-4850", website="printerland.co.uk")


def test_grounded_lookup_returns_a_usable_reading(config, logger):
    """A grounded lookup of a widely-stocked product must find a price.

    NOT_FOUND is deliberately *not* accepted here. It is the outcome the
    un-grounded path returns for everything, so allowing it would let this
    test pass green while proving nothing about grounding — the whole reason
    the test exists.
    """
    if not config.llm_config.grounded:
        pytest.skip("grounding disabled in config — cannot verify the grounded path")

    searcher = PriceSearcher(config.llm_config, config.price_ctrl, logger)
    reading = searcher.price(_LIVE_ITEM, None, datetime.now().replace(microsecond=0))

    assert reading.status in {
        PriceStatus.OK,
        PriceStatus.SUSPECT,
        PriceStatus.WRONG_CURRENCY,
    }, f"unexpected status {reading.status}: {reading.note}"

    if reading.status in {PriceStatus.OK, PriceStatus.SUSPECT}:
        assert reading.price is not None and reading.price > 0
        assert reading.source_url.startswith("http")


def test_ungrounded_lookup_completes_without_error(config, logger):
    """The un-grounded path must run cleanly on a key with no search quota.

    Forces grounding off regardless of config, so this covers the fallback
    whichever way the deployment is currently set. It asserts only that the
    two-call pipeline completes and grades itself — an un-grounded model has
    no live sources, so NOT_FOUND is the honest and expected result, and a
    price is not required. ERROR/PARSE_ERROR mean the plumbing broke.
    """
    llm_config = replace(config.llm_config, grounded=False)
    searcher = PriceSearcher(llm_config, config.price_ctrl, logger)
    reading = searcher.price(_LIVE_ITEM, None, datetime.now().replace(microsecond=0))

    assert reading.status not in {
        PriceStatus.ERROR,
        PriceStatus.PARSE_ERROR,
    }, f"un-grounded pipeline failed: {reading.note}"

    if reading.price is not None:
        assert reading.price > 0


def test_sheet_round_trip(config, logger, gsheet):
    from price_monitor.sheets.items_tab import ItemsTab

    items = ItemsTab(gsheet, config.price_ctrl.items_sheet, logger).read()
    assert isinstance(items, list)
    for item in items:
        assert item.name.strip()
        assert item.website.strip()


def test_history_round_trip_survives_trailing_empty_cells(config, logger, gsheet):
    """Reproduces, against the real Sheets API, the defect that motivated
    putting `status` last in COLUMNS: the API omits trailing empty cells from
    each row, and a naive column layout would either pad missing cells with
    NaN or (if every row in the sheet is short) raise ValueError out of
    GoogleSheetInterface.read() before HistoryTab ever sees the data. A
    not_found/error reading has both `source_url` and `note` blank — the
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
        website="shop.example",
        price=None,
        currency="GBP",
        status=PriceStatus.NOT_FOUND,
        source_url="",
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
        assert row["website"] == "shop.example"
        assert row["price"] == ""
        assert row["source_url"] == ""
        assert row["note"] == ""
        assert row["status"] == "not_found"
    finally:
        _delete_tab(gsheet, _SCRATCH_HISTORY_TAB, logger)


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
