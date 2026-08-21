"""Append-only price history tab.

Owns the long-format Prices tab: one row per item per run, plus the statistics
derived from it. Sheet values arrive as strings, so prices are coerced on read
and rendered back as strings on write.
"""

import logging

import pandas as pd
from py_utils.logger import get_child_logger

from price_monitor.models import PriceReading
from price_monitor.sheets.protocol import SheetInterface

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# `status` MUST stay last. The Sheets API omits trailing empty cells from each
# row, and when every row in a read comes back short, pandas does not pad —
# it raises inside GoogleSheetInterface.read() before this module ever sees
# the data, so there is nothing we can do to recover once that happens. Both
# `source_url` and `note` are blank on most readings, so if either sat last
# a fully-successful run's history could become permanently unreadable.
# `status` is always a non-empty PriceStatus value, so it anchors the row
# width. Do not "tidy" it back into the middle.
COLUMNS = [
    "timestamp",
    "item",
    "website",
    "price",
    "currency",
    "source_url",
    "note",
    "status",
]

SUMMARY_COLUMNS = ["item", "website", "current", "min", "mean", "last_checked"]


class HistoryTab:
    """Reads and appends the long-format price history."""

    def __init__(
        self, gsheet: SheetInterface, sheet_name: str, logger: logging.Logger
    ) -> None:
        self.logger = get_child_logger(logger, __class__.__name__)
        self._gsheet = gsheet
        self._sheet_name = sheet_name

    def read(self) -> pd.DataFrame:
        """Return the raw history frame, with all expected columns present."""
        frame = self._gsheet.read(sheet_name=self._sheet_name)
        if frame.empty:
            return pd.DataFrame(columns=COLUMNS)
        for column in COLUMNS:
            if column not in frame.columns:
                frame[column] = ""
        # Belt-and-braces: a manually-edited sheet can still come back with
        # mixed-width rows (some columns padded with NaN by pandas even
        # though every expected column is present). Left alone, a NaN would
        # be rendered as the literal string "nan" on the next write.
        return frame[COLUMNS].fillna("")

    def append(self, readings: list[PriceReading]) -> None:
        """Append readings to the tab, creating it on first use."""
        if not readings:
            return
        new_rows = pd.DataFrame([_to_row(r) for r in readings], columns=COLUMNS)
        existing = self.read()

        if existing.empty:
            # update() cannot create a missing tab; write() can, and there is no
            # history to lose on this path.
            self._gsheet.write(sheet_name=self._sheet_name, df=new_rows)
        else:
            # update() writes from A1 without clearing. The tab only ever grows,
            # so no clear is needed — and write()'s clear-then-write would risk
            # losing the entire history if the process died between the two.
            combined = pd.concat([existing, new_rows], ignore_index=True)
            self._gsheet.update(sheet_name=self._sheet_name, df=combined)

        self.logger.info(f"Appended {len(readings)} readings")

    def last_prices(self) -> dict[tuple[str, str], float]:
        """Most recent non-null price for each (item, website) pair."""
        frame = self._typed()
        prices: dict[tuple[str, str], float] = {}
        if frame.empty:
            return prices
        priced = frame[frame["price"].notna()]
        for key, group in priced.groupby(["item", "website"], sort=False):
            ordered = group.sort_values("timestamp", kind="stable")
            prices[key] = float(ordered["price"].iloc[-1])
        return prices

    def known_items(self) -> set[tuple[str, str]]:
        """Every (item, website) pair that has ever been recorded."""
        frame = self.read()
        if frame.empty:
            return set()
        return set(zip(frame["item"], frame["website"]))

    def summarise(self) -> pd.DataFrame:
        """Per-item current, min, mean, and last-checked timestamp."""
        frame = self._typed()
        if frame.empty:
            return pd.DataFrame(columns=SUMMARY_COLUMNS)

        rows = []
        for (item, website), group in frame.groupby(["item", "website"], sort=False):
            ordered = group.sort_values("timestamp", kind="stable")
            priced = ordered[ordered["price"].notna()]
            rows.append(
                {
                    "item": item,
                    "website": website,
                    "current": (
                        float(priced["price"].iloc[-1]) if not priced.empty else ""
                    ),
                    "min": float(priced["price"].min()) if not priced.empty else "",
                    "mean": (
                        round(float(priced["price"].mean()), 2)
                        if not priced.empty
                        else ""
                    ),
                    # Any status, so a run of failures shows as a moving
                    # timestamp beside a stale price rather than looking healthy.
                    "last_checked": ordered["timestamp"].iloc[-1],
                }
            )
        return pd.DataFrame(rows, columns=SUMMARY_COLUMNS)

    def _typed(self) -> pd.DataFrame:
        """History with the price column coerced to numbers, blanks becoming NaN."""
        frame = self.read()
        if frame.empty:
            return frame
        frame = frame.copy()
        frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
        return frame


def _to_row(reading: PriceReading) -> dict[str, str]:
    return {
        "timestamp": reading.timestamp.strftime(TIMESTAMP_FORMAT),
        "item": reading.item,
        "website": reading.website,
        "price": "" if reading.price is None else f"{reading.price:.2f}",
        "currency": reading.currency,
        "source_url": reading.source_url,
        "note": reading.note,
        "status": reading.status.value,
    }
