"""User-facing Items tab.

Columns A-C (``item``, ``website``, ``market``) are owned by the user; the
summary columns after them are rewritten on every run, split between the
per-supplier statistics and the opt-in UK-wide market sweep. Malformed rows
are skipped rather than allowed to reach the LLM.
"""

import logging

import pandas as pd
from py_utils.logger import get_child_logger

from price_monitor.models import MARKET_KEY, Item
from price_monitor.sheets.protocol import SheetInterface

# `market_checked` MUST stay last, for the same reason `status` anchors the
# history tab: the Sheets API omits trailing empty cells from each row, and
# when every row in a read comes back short, pandas raises inside
# GoogleSheetInterface.read() before this module ever sees the data. An
# opted-out item leaves best_price/best_supplier/best_supplier_rating/
# confidence all blank, so market_checked (always "never" or a timestamp)
# is what keeps such rows from truncating.
COLUMNS = [
    "item",
    "website",
    "market",
    "current",
    "min",
    "mean",
    "last_checked",
    "best_price",
    "best_supplier",
    "best_supplier_rating",
    "confidence",
    "sources",
    "market_checked",
]
_SUMMARY_FIELDS = ["current", "min", "mean", "last_checked"]
_MARKET_FIELDS = [
    "best_price",
    "best_supplier",
    "best_supplier_rating",
    "confidence",
    "sources",
]

_TRUTHY = frozenset({"yes", "y", "true", "1"})


class ItemsTab:
    """Reads tracked items and writes their summary statistics back."""

    def __init__(
        self, gsheet: SheetInterface, sheet_name: str, logger: logging.Logger
    ) -> None:
        self.logger = get_child_logger(logger, __class__.__name__)
        self._gsheet = gsheet
        self._sheet_name = sheet_name

    def read(self) -> list[Item]:
        """Return the valid, de-duplicated items listed on the tab."""
        frame = self._gsheet.read(sheet_name=self._sheet_name)
        if frame.empty or "item" not in frame.columns or "website" not in frame.columns:
            return []

        items: list[Item] = []
        seen: set[tuple[str, str]] = set()
        for position, row in enumerate(frame.to_dict("records"), start=2):
            # pd.isna() handles None, NaN, NaT; bool(float('nan')) is True so
            # the falsy check would not catch pandas' missing value representation.
            if pd.isna(row.get("item")) or pd.isna(row.get("website")):
                self.logger.warning(f"Row {position}: blank item or website, skipping")
                continue
            name = str(row.get("item")).strip()
            website = str(row.get("website")).strip()
            if not name or not website:
                self.logger.warning(f"Row {position}: blank item or website, skipping")
                continue
            if website.lower() == MARKET_KEY:
                self.logger.warning(
                    f"Row {position}: '{MARKET_KEY}' is reserved, skipping"
                )
                continue
            key = (name, website)
            if key in seen:
                self.logger.warning(
                    f"Row {position}: duplicate ('{name}', '{website}'), skipping"
                )
                continue
            seen.add(key)
            # row.get("market") is None on a pre-upgrade sheet that has no
            # market column at all, which correctly defaults it to opted-out.
            market = str(row.get("market") or "").strip().lower() in _TRUTHY
            items.append(Item(name=name, website=website, market=market))
        return items

    def write_summary(
        self, items: list[Item], summary: pd.DataFrame, market: pd.DataFrame
    ) -> None:
        """Rewrite the tab with each item's latest supplier and market figures."""
        lookup: dict[tuple[str, str], dict] = {}
        if not summary.empty:
            lookup = {
                (row["item"], row["website"]): row for row in summary.to_dict("records")
            }
        # Keyed by item name only: a market sweep is not tied to a supplier.
        market_lookup: dict[str, dict] = {}
        if not market.empty:
            market_lookup = {row["item"]: row for row in market.to_dict("records")}

        rows = []
        for item in items:
            stats = lookup.get((item.name, item.website), {})
            swept = market_lookup.get(item.name, {})
            row = {
                "item": item.name,
                "website": item.website,
                "market": "yes" if item.market else "",
            }
            for field in _SUMMARY_FIELDS:
                row[field] = stats.get(field, "")
            for field in _MARKET_FIELDS:
                row[field] = swept.get(field, "")
            row["market_checked"] = swept.get("market_checked", "")
            # "never" (not "") anchors the row's final column, and reads
            # more clearly than a blank cell, which is ambiguous between
            # "not checked yet" and "something went wrong".
            if not row["last_checked"]:
                row["last_checked"] = "never"
            if not row["market_checked"]:
                row["market_checked"] = "never"
            rows.append(row)

        frame = pd.DataFrame(rows, columns=COLUMNS)
        # write(), not update(): this tab shrinks when the user deletes an item,
        # and without the clear a deletion would leave a duplicated last row.
        self._gsheet.write(sheet_name=self._sheet_name, df=frame)
        self.logger.info(f"Wrote summary for {len(rows)} items")
