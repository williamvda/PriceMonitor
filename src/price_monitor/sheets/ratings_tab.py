"""Cached retailer ratings.

One row per retailer, refreshed on its own slow cadence rather than on every
sweep. A row with a ``checked`` date and no rating records that the retailer
was looked up and had none — a different state from never having asked, which
is a site being absent from the tab entirely.
"""

import logging
from datetime import datetime, timedelta
from typing import Callable

import pandas as pd
from py_utils.logger import get_child_logger

from price_monitor.models import Rating
from price_monitor.sheets.protocol import SheetInterface

DATE_FORMAT = "%Y-%m-%d"

# `checked` MUST stay last. The Sheets API omits trailing empty cells, and when
# every row in a read comes back short pandas raises inside
# GoogleSheetInterface.read() before this module sees it. Every other column is
# blank for a site that has been registered but not yet rated.
COLUMNS = ["site", "rating", "reviews", "source", "checked"]


class RatingsTab:
    """Reads and maintains the cached ratings, never writing over what it can't read."""

    def __init__(
        self,
        gsheet: SheetInterface,
        sheet_name: str,
        logger: logging.Logger,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self.logger = get_child_logger(logger, __class__.__name__)
        self._gsheet = gsheet
        self._sheet_name = sheet_name
        self._notify = notify or (lambda _message: None)

    def read(self) -> dict[str, Rating] | None:
        """Cached ratings by site, or ``None`` when the tab could not be read."""
        frame = self._frame()
        if frame is None:
            return None
        return {
            site: Rating(
                rating=_as_float(row["rating"]),
                reviews=_as_int(row["reviews"]),
                source=str(row["source"] or "").strip(),
            )
            for row in frame.to_dict("records")
            if (site := str(row["site"] or "").strip())
        }

    def stale(self, within_days: float) -> list[str]:
        """Sites needing a lookup, never-checked ones first.

        A site we have never rated is worth more than refreshing one we already
        have a usable number for, so the batch cap spends itself on the former.
        """
        frame = self._frame()
        if frame is None:
            return []
        cutoff = datetime.now() - timedelta(days=within_days)
        never, old = [], []
        for row in frame.to_dict("records"):
            site = str(row["site"] or "").strip()
            if not site:
                continue
            checked = _as_date(row["checked"])
            if checked is None:
                never.append(site)
            elif checked < cutoff:
                old.append(site)
        return never + old

    def register(self, sites: set[str]) -> None:
        """Add placeholder rows for sites the cache has not seen."""
        frame = self._frame()
        if frame is None:
            return
        known = {str(site).strip() for site in frame["site"]}
        unseen = sorted({s.strip() for s in sites if s.strip()} - known)
        if not unseen:
            return
        additions = pd.DataFrame(
            [{**{column: "" for column in COLUMNS}, "site": site} for site in unseen],
            columns=COLUMNS,
        )
        self._write(pd.concat([frame, additions], ignore_index=True))
        self.logger.info(f"Registered {len(unseen)} new retailer(s) for rating")

    def store(self, results: dict[str, Rating]) -> None:
        """Record a lookup's results, stamping ``checked`` for every site asked.

        A site that came back without a rating is stamped too: otherwise a
        retailer with no Trustpilot or Google presence would be looked up on
        every refresh, forever.
        """
        frame = self._frame()
        if frame is None or not results:
            return
        today = datetime.now().strftime(DATE_FORMAT)
        rows = {str(row["site"]).strip(): dict(row) for row in frame.to_dict("records")}
        for site, rating in results.items():
            rows[site] = {
                "site": site,
                "rating": "" if rating.rating is None else f"{rating.rating:g}",
                "reviews": "" if rating.reviews is None else str(rating.reviews),
                "source": rating.source,
                "checked": today,
            }
        self._write(pd.DataFrame(list(rows.values()), columns=COLUMNS))

    def _frame(self) -> pd.DataFrame | None:
        """The tab's contents, or ``None`` when it could not be read.

        A tab that does not exist fails in the Sheets API, which cannot parse
        the range — that is a first run, and creating it is safe. A tab whose
        every row is short of its header fails in pandas, locally, which means
        it exists and still holds data. ``write()`` clears before it writes, so
        that one must never be written to.
        """
        try:
            frame = self._gsheet.read(sheet_name=self._sheet_name)
        except ValueError as exc:
            message = (
                f"The '{self._sheet_name}' tab could not be read ({exc}), so no "
                f"cached ratings are being used and none will be written."
            )
            self.logger.warning(message)
            self._notify(f"⚠️ PriceMonitor — {message}")
            return None
        except Exception as exc:
            self.logger.info(f"No '{self._sheet_name}' tab ({exc}); creating it.")
            self._write(pd.DataFrame(columns=COLUMNS))
            return pd.DataFrame(columns=COLUMNS)

        if frame.empty:
            return pd.DataFrame(columns=COLUMNS)
        for column in COLUMNS:
            if column not in frame.columns:
                frame[column] = ""
        return frame[COLUMNS].fillna("")

    def _write(self, frame: pd.DataFrame) -> None:
        try:
            self._gsheet.write(sheet_name=self._sheet_name, df=frame)
        except Exception as exc:
            self.logger.warning(f"Could not write '{self._sheet_name}': {exc}")


def _as_float(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _as_date(value: object) -> datetime | None:
    try:
        return datetime.strptime(str(value).strip(), DATE_FORMAT)
    except (TypeError, ValueError):
        return None
