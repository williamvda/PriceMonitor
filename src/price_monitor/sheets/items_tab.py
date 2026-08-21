"""User-facing Items tab.

Columns A-B (``item``, ``website``) are owned by the user; the summary columns
after them are rewritten on every run. Malformed rows are skipped rather than
allowed to reach the LLM.
"""

import logging

import pandas as pd
from py_utils.logger import get_child_logger

from price_monitor.models import Item
from price_monitor.sheets.protocol import SheetInterface

COLUMNS = ["item", "website", "current", "min", "mean", "last_checked"]
_SUMMARY_FIELDS = ["current", "min", "mean", "last_checked"]


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
            name = str(row.get("item") or "").strip()
            website = str(row.get("website") or "").strip()
            if not name or not website:
                self.logger.warning(f"Row {position}: blank item or website, skipping")
                continue
            key = (name, website)
            if key in seen:
                self.logger.warning(f"Row {position}: duplicate '{name}', skipping")
                continue
            seen.add(key)
            items.append(Item(name=name, website=website))
        return items

    def write_summary(self, items: list[Item], summary: pd.DataFrame) -> None:
        """Rewrite the tab with each item's latest statistics."""
        lookup: dict[tuple[str, str], dict] = {}
        if not summary.empty:
            lookup = {
                (row["item"], row["website"]): row for row in summary.to_dict("records")
            }

        rows = []
        for item in items:
            stats = lookup.get((item.name, item.website), {})
            row = {"item": item.name, "website": item.website}
            for field in _SUMMARY_FIELDS:
                row[field] = stats.get(field, "")
            rows.append(row)

        frame = pd.DataFrame(rows, columns=COLUMNS)
        # write(), not update(): this tab shrinks when the user deletes an item,
        # and without the clear a deletion would leave a duplicated last row.
        self._gsheet.write(sheet_name=self._sheet_name, df=frame)
        self.logger.info(f"Wrote summary for {len(rows)} items")
