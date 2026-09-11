"""Append-only price history tab.

Owns the long-format Prices tab: one wide row per sweep, carrying the three
detections it produced, plus the statistics derived from them. Only the
cheapest series has a mean — the best-rated retailer's price says more about
that retailer than about the market. Sheet values arrive as strings, so prices
are coerced on read and rendered back as strings on write.
"""

import logging
from datetime import datetime

import pandas as pd
from py_utils.logger import get_child_logger

from price_monitor.models import Detection, PriceReading, PriceStats
from price_monitor.sheets.protocol import SheetInterface

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# `status` MUST stay last. The Sheets API omits trailing empty cells from each
# row, and when every row in a read comes back short, pandas does not pad —
# it raises inside GoogleSheetInterface.read() before this module ever sees
# the data, so there is nothing we can do to recover once that happens. Every
# other column here is blank on a failed sweep: all nine detection cells, the
# confidence, the source count and often the note. `status` is always a
# non-empty PriceStatus value, so it alone anchors the row width. Do not
# "tidy" it back into the middle.
COLUMNS = [
    "timestamp",
    "item",
    "rated_price",
    "rated_site",
    "rated_rating",
    "cheapest_price",
    "cheapest_site",
    "cheapest_rating",
    "value_price",
    "value_site",
    "value_rating",
    "confidence",
    "sources",
    "note",
    "status",
]

SUMMARY_COLUMNS = [
    "item",
    "rated_price",
    "rated_site",
    "rated_rating",
    "cheapest_price",
    "cheapest_site",
    "cheapest_rating",
    "value_price",
    "value_site",
    "value_rating",
    "min",
    "mean",
    "confidence",
    "sources",
    "last_checked",
]

_DETECTION_PREFIXES = ("rated", "cheapest", "value")
_DETECTION_FIELDS = [
    f"{prefix}_{suffix}"
    for prefix in _DETECTION_PREFIXES
    for suffix in ("price", "site", "rating")
]


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

    def last_prices(self) -> dict[str, float]:
        """Most recent non-null cheapest price for each item."""
        frame = self._typed()
        prices: dict[str, float] = {}
        if frame.empty:
            return prices
        priced = frame[frame["cheapest_price"].notna()]
        for item, group in priced.groupby("item", sort=False):
            ordered = group.sort_values("timestamp", kind="stable")
            prices[item] = float(ordered["cheapest_price"].iloc[-1])
        return prices

    def price_stats(self) -> dict[str, PriceStats]:
        """Mean, most recent cheapest price, and reading count per item.

        Only rows that carry a cheapest price count: a failed sweep must not
        drag the mean or masquerade as the last price. Items with no priced row
        at all are absent rather than present-and-empty.
        """
        frame = self._typed()
        if frame.empty:
            return {}
        stats: dict[str, PriceStats] = {}
        priced = frame[frame["cheapest_price"].notna()]
        for item, group in priced.groupby("item", sort=False):
            ordered = group.sort_values("timestamp", kind="stable")
            stats[item] = PriceStats(
                mean=float(ordered["cheapest_price"].mean()),
                last=float(ordered["cheapest_price"].iloc[-1]),
                count=len(ordered),
            )
        return stats

    def last_checked(self) -> dict[str, datetime]:
        """Most recent sweep time for each item, any status.

        Any status counts: a failed sweep still spent an API call, so it must
        still hold off the next one. Rows whose timestamp does not parse are
        ignored, which leaves the item looking unchecked — a hand-edited sheet
        should cause an extra refresh, never silently suppress one.
        """
        frame = self.read()
        if frame.empty:
            return {}
        frame = frame.copy()
        frame["_checked"] = pd.to_datetime(
            frame["timestamp"], format=TIMESTAMP_FORMAT, errors="coerce"
        )
        frame = frame[frame["_checked"].notna()]
        return {
            item: group["_checked"].max().to_pydatetime()
            for item, group in frame.groupby("item", sort=False)
        }

    def known_items(self) -> set[str]:
        """Every item that has ever been swept."""
        frame = self.read()
        if frame.empty:
            return set()
        return set(frame["item"])

    def summarise(self) -> pd.DataFrame:
        """Each item's latest detections, its cheapest min/mean, and when.

        The detection cells come from the latest *priced* sweep while
        ``last_checked`` moves on every sweep of any status, so a run of
        failures shows as a moving timestamp beside a stale price rather than
        looking healthy.
        """
        frame = self._typed()
        if frame.empty:
            return pd.DataFrame(columns=SUMMARY_COLUMNS)

        rows = []
        for item, group in frame.groupby("item", sort=False):
            ordered = group.sort_values("timestamp", kind="stable")
            priced = ordered[ordered["cheapest_price"].notna()]
            latest = priced.iloc[-1] if not priced.empty else None
            row = {"item": item}
            for field in _DETECTION_FIELDS:
                value = "" if latest is None else latest[field]
                row[field] = _price_cell(value) if field.endswith("_price") else value
            row["min"] = (
                float(priced["cheapest_price"].min()) if not priced.empty else ""
            )
            row["mean"] = (
                round(float(priced["cheapest_price"].mean()), 2)
                if not priced.empty
                else ""
            )
            row["confidence"] = "" if latest is None else str(latest["confidence"])
            row["sources"] = "" if latest is None else str(latest["sources"])
            row["last_checked"] = ordered["timestamp"].iloc[-1]
            rows.append(row)
        return pd.DataFrame(rows, columns=SUMMARY_COLUMNS)

    def _typed(self) -> pd.DataFrame:
        """History with the cheapest price coerced to numbers, blanks to NaN."""
        frame = self.read()
        if frame.empty:
            return frame
        frame = frame.copy()
        frame["cheapest_price"] = pd.to_numeric(
            frame["cheapest_price"], errors="coerce"
        )
        return frame


def _price_cell(value: object) -> float | str:
    """A recorded price as a number, or blank when the sweep had none."""
    number = pd.to_numeric(value, errors="coerce")
    return "" if pd.isna(number) else float(number)


def _detection_cells(prefix: str, detection: Detection | None) -> dict[str, str]:
    if detection is None:
        return {f"{prefix}_price": "", f"{prefix}_site": "", f"{prefix}_rating": ""}
    return {
        f"{prefix}_price": f"{detection.price:.2f}",
        f"{prefix}_site": detection.site,
        f"{prefix}_rating": detection.rating,
    }


def _to_row(reading: PriceReading) -> dict[str, str]:
    row = {
        "timestamp": reading.timestamp.strftime(TIMESTAMP_FORMAT),
        "item": reading.item,
        "confidence": (
            "" if reading.confidence is None else f"{reading.confidence:.2f}"
        ),
        "sources": "" if reading.sources is None else str(reading.sources),
        "note": reading.note,
        "status": reading.status.value,
    }
    row.update(_detection_cells("rated", reading.best_rated))
    row.update(_detection_cells("cheapest", reading.cheapest))
    row.update(_detection_cells("value", reading.best_value))
    return row
