"""User-facing Items tab.

Column A (``item``) is owned by the user and is the only thing they type; the
summary columns after it are rewritten on every run with the latest sweep's
three detections and the statistics over its cheapest series. Malformed rows
are skipped rather than allowed to reach the LLM.
"""

import logging

import pandas as pd
from py_utils.logger import get_child_logger

from price_monitor.models import Item
from price_monitor.sheets.history_tab import SUMMARY_COLUMNS
from price_monitor.sheets.protocol import SheetInterface

# `last_checked` MUST stay last, for the same reason `status` anchors the
# history tab: the Sheets API omits trailing empty cells from each row, and
# when every row in a read comes back short, pandas raises inside
# GoogleSheetInterface.read() before this module ever sees the data. Every
# column from `rated_price` to `sources` is blank for an item that has never
# been swept, so `last_checked` — always "never" or a timestamp — is the only
# thing holding such a row's width. It is not cosmetic and must not be moved.
# Imported, not retyped: summarise() produces exactly this shape and
# write_summary() writes it verbatim, so a second copy could only drift.
COLUMNS = SUMMARY_COLUMNS

_SUMMARY_FIELDS = COLUMNS[1:-1]


class ItemsTab:
    """Reads tracked items and writes their summary statistics back."""

    def __init__(
        self, gsheet: SheetInterface, sheet_name: str, logger: logging.Logger
    ) -> None:
        self.logger = get_child_logger(logger, __class__.__name__)
        self._gsheet = gsheet
        self._sheet_name = sheet_name

    def read(self) -> list[Item]:
        """Return the valid, de-duplicated items listed on the tab.

        Only the ``item`` column is read, so a sheet still carrying the old
        ``website`` and ``market`` columns migrates by being ignored — the
        first summary write rewrites the tab in the new shape.
        """
        frame = self._gsheet.read(sheet_name=self._sheet_name)
        if frame.empty or "item" not in frame.columns:
            return []

        items: list[Item] = []
        seen: set[str] = set()
        for position, row in enumerate(frame.to_dict("records"), start=2):
            # pd.isna() handles None, NaN, NaT; bool(float('nan')) is True so
            # the falsy check would not catch pandas' missing value representation.
            if pd.isna(row.get("item")):
                self.logger.warning(f"Row {position}: blank item, skipping")
                continue
            name = str(row.get("item")).strip()
            if not name:
                self.logger.warning(f"Row {position}: blank item, skipping")
                continue
            if name in seen:
                self.logger.warning(f"Row {position}: duplicate '{name}', skipping")
                continue
            seen.add(name)
            items.append(Item(name=name))
        return items

    def write_summary(self, items: list[Item], summary: pd.DataFrame) -> None:
        """Rewrite the tab with each item's latest detections and statistics."""
        lookup: dict[str, dict] = {}
        if not summary.empty:
            lookup = {row["item"]: row for row in summary.to_dict("records")}

        rows = []
        for item in items:
            stats = lookup.get(item.name, {})
            row = {"item": item.name}
            for field in _SUMMARY_FIELDS:
                row[field] = stats.get(field, "")
            row["last_checked"] = stats.get("last_checked", "")
            # "never" (not "") anchors the row's final column, and reads more
            # clearly than a blank cell, which is ambiguous between "not
            # checked yet" and "something went wrong". Every other column is
            # blank for an item that has never been swept, so this cell alone
            # keeps the row from being trimmed to nothing on the next read.
            if not row["last_checked"]:
                row["last_checked"] = "never"
            rows.append(row)

        frame = pd.DataFrame(rows, columns=COLUMNS)
        # write(), not update(): this tab shrinks when the user deletes an item,
        # and without the clear a deletion would leave a duplicated last row.
        self._gsheet.write(sheet_name=self._sheet_name, df=frame)
        self.logger.info(f"Wrote summary for {len(rows)} items")
