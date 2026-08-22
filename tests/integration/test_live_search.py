"""Live checks against the real Gemini API and Google Sheets.

Run with: pytest -m integration
Requires PRICE_MONITOR_SECRETS to point at a configured secrets directory.
"""

import logging
from dataclasses import replace
from datetime import datetime

import pandas as pd
import pytest
from llmbridge import LLMClient

from price_monitor.models import Item, PriceReading, PriceStatus
from price_monitor.search.gemini_search import grounding_urls
from price_monitor.search.prompts import search_prompt
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


class _Replay:
    """Serves one already-fetched response, so call two runs for free."""

    def __init__(self, response) -> None:
        self._response = response

    def prompt(self, prompt_text: str, **kwargs):
        return self._response


def test_grounding_is_live_and_the_pipeline_grades_the_result(config, logger):
    """Grounding executed, and the two-call pipeline graded what came back.

    The load-bearing assertion is the presence of grounding citations, not a
    price. ``groundingMetadata`` cannot appear unless the search tool actually
    ran, so it goes red exactly when something real breaks — quota withdrawn,
    the tool key wrong, the provider regressed — and stays green regardless of
    whether one retailer happened to list a price today.

    An earlier version required OK/SUSPECT/WRONG_CURRENCY. It failed on live
    stock levels rather than on defects: the same item passed in the morning
    and failed the same afternoon on unchanged code. A test that cannot tell
    a breakage from a thin search index is not measuring this codebase.

    Call one is issued directly so its raw response can be inspected, then
    replayed into the searcher — the real format call and validation ladder
    still run, and only one grounded search is spent.
    """
    if not config.llm_config.grounded:
        pytest.skip("grounding disabled in config — cannot verify the grounded path")

    llm = config.llm_config
    searched = LLMClient(
        provider=llm.provider,
        api_key=llm.api_key,
        model=llm.model,
        max_tokens=llm.max_tokens,
        temperature=llm.temperature,
        timeout=llm.timeout,
    ).prompt(search_prompt(_LIVE_ITEM))

    assert searched.text and searched.text.strip(), "grounded call returned no text"
    assert grounding_urls(searched.raw_response), (
        "no grounding citations returned — the search tool did not run. "
        f"Reply was: {(searched.text or '')[:200]}"
    )

    reading = PriceSearcher(
        llm, config.price_ctrl, logger, search_client=_Replay(searched)
    ).price(_LIVE_ITEM, None, datetime.now().replace(microsecond=0))

    assert reading.status not in {
        PriceStatus.ERROR,
        PriceStatus.PARSE_ERROR,
    }, f"pipeline failed on a grounded reply: {reading.note}"

    if reading.price is not None:
        assert reading.price > 0
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
