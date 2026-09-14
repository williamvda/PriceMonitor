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
from price_monitor.sheets.protocol import SheetInterface, is_missing_tab_error

DATE_FORMAT = "%Y-%m-%d"

# A rating enters here from two places — the automated lookup and a human
# correcting it by hand, which the tab explicitly invites when the model has
# reported a retailer three different ways. Neither entrance can vouch for the
# other, so this guard duplicates search/ratings.py's rather than trusting a
# value that already passed it once.
_MAX_RATING = 5.0

# `checked` MUST stay last. The Sheets API omits trailing empty cells, and when
# every row in a read comes back short pandas raises inside
# GoogleSheetInterface.read() before this module sees it. Every other column is
# blank for a site that has been registered but not yet rated.
COLUMNS = ["site", "rating", "reviews", "source", "checked"]

# A registered-but-unrated row must never leave ``checked`` blank. The Sheets
# API omits trailing empty cells, so a tab holding only placeholder rows comes
# back one cell wide against a five-column header and pandas raises inside
# GoogleSheetInterface.read() — bricking every method here, including the one
# that would have written the value that repairs it. "never" is not a date, and
# _as_date already reads anything unparseable as "look it up again".
NEVER = "never"


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
        # update() calls _frame() up to three times (stale, read, register);
        # this suppresses the second and third alert for the same failure
        # within a run. Reset on the next successful read, so a tab that
        # recovers and later breaks again still gets a fresh alert.
        self._unreadable_notified = False

    def read(self) -> dict[str, Rating] | None:
        """Cached ratings by site, or ``None`` when the tab could not be read."""
        frame = self._frame()
        if frame is None:
            return None
        return {
            site: Rating(
                rating=_clamped(_as_float(row["rating"])),
                reviews=_as_int(row["reviews"]),
                source=str(row["source"] or "").strip(),
            )
            for row in frame.to_dict("records")
            if (site := str(row["site"] or "").strip().lower())
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
            site = str(row["site"] or "").strip().lower()
            if not site:
                continue
            checked = _as_date(row["checked"])
            # A future date cannot have been written honestly — it is a
            # hand-edit error, and the safe direction for an error is always
            # "look it up again", never "frozen until real time catches up".
            if checked is None or checked > datetime.now():
                never.append(site)
            elif checked < cutoff:
                old.append(site)
        return never + old

    def register(self, sites: set[str]) -> None:
        """Add placeholder rows for sites the cache has not seen.

        Only the three winning detections reach the price history, so a
        retailer that competed and lost is written down nowhere else. Without
        this, nothing would ever know to fetch its rating, and it could never
        become one of the winners.
        """
        frame = self._frame()
        if frame is None:
            return
        known = {str(site).strip().lower() for site in frame["site"]}
        unseen = sorted({s.strip().lower() for s in sites if s.strip()} - known)
        if not unseen:
            return
        additions = pd.DataFrame(
            [
                {**{column: "" for column in COLUMNS}, "site": site, "checked": NEVER}
                for site in unseen
            ],
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
        rows = {
            str(row["site"]).strip().lower(): dict(row)
            for row in frame.to_dict("records")
        }
        for raw_site, rating in results.items():
            site = str(raw_site).strip().lower()
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

        A missing tab is told apart from every other failure by the Sheets
        API's message, not by exception type (see :func:`is_missing_tab_error`)
        — that is a first run, and creating it is safe. Anything else — a
        5xx, a timeout, an auth failure, or a tab whose every row is short of
        its header (which fails in pandas, locally) — means the tab exists
        and still holds data, so it must never be written: ``write()`` clears
        before it writes. ``update()`` calls this up to three times per run,
        so the *alert* for that case is deduplicated via
        ``self._unreadable_notified``; the warning is logged every time.
        """
        try:
            frame = self._gsheet.read(sheet_name=self._sheet_name)
        except Exception as exc:
            if is_missing_tab_error(exc):
                self.logger.info(f"No '{self._sheet_name}' tab ({exc}); creating it.")
                self._write(pd.DataFrame(columns=COLUMNS))
                return pd.DataFrame(columns=COLUMNS)
            message = (
                f"The '{self._sheet_name}' tab could not be read ({exc}), so "
                f"no cached ratings are being used and none will be written."
            )
            # Logged every time: the log is the record of what actually
            # happened, and a failure that changes between runs matters.
            # Only the alert is deduplicated.
            self.logger.warning(message)
            if not self._unreadable_notified:
                self._notify(f"⚠️ PriceMonitor — {message}")
                self._unreadable_notified = True
            return None

        self._unreadable_notified = False
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


def _clamped(rating: float | None) -> float | None:
    """``rating`` if it is a trustworthy five-point value, else ``None``."""
    if rating is not None and not (0.0 <= rating <= _MAX_RATING):
        return None
    return rating


def _as_float(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    try:
        return int(float(str(value).strip().replace(",", "")))
    except (TypeError, ValueError):
        return None


def _as_date(value: object) -> datetime | None:
    try:
        return datetime.strptime(str(value).strip(), DATE_FORMAT)
    except (TypeError, ValueError):
        return None
