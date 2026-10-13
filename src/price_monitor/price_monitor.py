"""PriceMonitor controller and CLI entry point.

Owns timing and orchestration: a slow full refresh of every tracked item
(``refresh_rate_h``) and a faster poll that sweeps only items newly added to
the sheet (``poll_rate_m``). One UK-wide sweep per item, whose three detections
are recorded and whose cheapest price drives the notifications. All state lives
in the spreadsheet, so a restart loses nothing. Neither JSON parsing nor
sheet-range addressing belongs here —
those live in :mod:`price_monitor.app_config` and :mod:`price_monitor.sheets`.
"""

import argparse
import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from google_drive_api import GoogleSheetInterface
from py_utils.logger import consoleLogger, get_child_logger

from price_monitor import __version__
from price_monitor.app_config import PriceCtrl, load_price_config
from price_monitor.models import Item, PriceReading, PriceStats, PriceStatus, Rating
from price_monitor.msgserver_client.msg_forwarder import (
    MsgNotifier,
    attach_msg_server,
    detach_msg_server,
    format_check_complete,
    format_check_failed,
    format_price_drop,
    format_startup_message,
)
from price_monitor.search.market import MarketSearcher
from price_monitor.search.ratings import RATING_BATCH, RatingsLookup
from price_monitor.search.searcher import SpendCapReached
from price_monitor.sheets.control_tab import ControlTab
from price_monitor.sheets.history_tab import HistoryTab
from price_monitor.sheets.items_tab import ItemsTab
from price_monitor.sheets.protocol import MonitoredSheetInterface
from price_monitor.sheets.ratings_tab import RatingsTab

# Wake at least this often so stop() is honoured promptly even when the next
# scheduled event is hours away.
_MAX_SLEEP_S = 20.0


class PriceMonitor:
    """Polls a sheet of tracked items and records their prices over time."""

    def __init__(
        self,
        secrets: Path,
        logger: logging.Logger,
        msg_server: bool = False,
    ) -> None:
        self.logger = get_child_logger(logger, self.__class__.__name__)
        # No load_dotenv() here: py_utils.load_config reads secrets/.env
        # directly via dotenv_values() for ENCRYPTION_KEY, so nothing needs
        # loading into the process environment at this call site.
        config = load_price_config(secrets / "config.json")

        # Attached to the root logger, not the child, so records from every
        # component propagate into it. Purely additive — console output is
        # untouched whether or not the server is reachable.
        self.root_logger = logger
        self.msg_client = (
            attach_msg_server(logger, config.msg_config) if msg_server else None
        )
        self.notifier = MsgNotifier(
            self.msg_client, handle=config.msg_config.handle, logger=self.logger
        )

        self.ctrl: PriceCtrl = config.price_ctrl
        gsheet = GoogleSheetInterface(config=config.drive_config, logger=logger)
        self.ratings_lookup = RatingsLookup(config.llm_config, logger)
        self._init_parts(
            gsheet=gsheet,
            searcher=MarketSearcher(
                config.llm_config,
                config.price_ctrl,
                logger,
                report_dir=secrets / "reports",
            ),
        )

    @classmethod
    def for_test(
        cls,
        gsheet: MonitoredSheetInterface,
        searcher: MarketSearcher,
        logger: logging.Logger,
    ) -> "PriceMonitor":
        """Build a controller around fakes, skipping config and network setup."""
        monitor = cls.__new__(cls)
        monitor.logger = get_child_logger(logger, cls.__name__)
        monitor.ctrl = PriceCtrl(request_delay_s=0.0)
        monitor.root_logger = logger
        monitor.msg_client = None
        monitor.notifier = MsgNotifier(client=None, logger=monitor.logger)
        # No lookup client for tests that don't care: _refresh_ratings treats
        # None as "never make a call".
        monitor.ratings_lookup = None
        monitor._init_parts(gsheet=gsheet, searcher=searcher)
        return monitor

    def _init_parts(
        self,
        gsheet: MonitoredSheetInterface,
        searcher: MarketSearcher,
    ) -> None:
        self._searcher = searcher
        self.items_tab = ItemsTab(gsheet, self.ctrl.items_sheet, self.logger)
        self.history = HistoryTab(gsheet, self.ctrl.history_sheet, self.logger)
        self.control_tab = ControlTab(
            gsheet,
            self.ctrl.control_sheet,
            self.logger,
            notify=self.notifier.notify,
        )
        self.ratings_tab = RatingsTab(
            gsheet, self.ctrl.ratings_sheet, self.logger, notify=self.notifier.notify
        )
        self._gsheet = gsheet
        self.paused_until: datetime | None = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="PriceMonitor")

    def update(self, force: bool = False) -> None:
        """Price tracked items due for a refresh and rewrite the summary.

        An item checked less than ``refresh_rate_h`` ago is skipped: each
        sweep costs a billed search, and repeated CLI runs would otherwise
        re-price the same items for no new information. ``force`` re-prices
        everything regardless.
        """
        items = self.items_tab.read()
        if not items:
            self.logger.info("No items to price")
            return
        self._warn_orphaned_history(items)

        due = items if force else self._due_for_refresh(items)
        if skipped := len(items) - len(due):
            self.logger.info(
                f"Skipping {skipped} item(s) checked within the last "
                f"{self.ctrl.refresh_rate_h}h"
            )
        if not due:
            return

        self.logger.info(f"Pricing {len(due)} items")
        # Ratings before the sweeps: this run's sweeps read the cache the
        # refresh just filled, so filling it afterwards would waste a day.
        self._refresh_ratings()
        if self.paused_until:
            return
        self._price_and_record("update", due)

    def _refresh_ratings(self) -> None:
        """Rate up to one batch of unrated or stale retailers.

        Bounded to a single grounded call per run whatever state the cache is
        in, so a first run against many retailers spreads over days rather
        than spending a search on each at once. A failure here logs and
        returns: ratings are a nicety, prices are the job.
        """
        if self.ratings_lookup is None:
            return
        try:
            stale = self.ratings_tab.stale(self.ctrl.rating_rate_d)[:RATING_BATCH]
            if not stale:
                return
            self.logger.info(f"Rating {len(stale)} retailer(s)")
            self.ratings_tab.store(self.ratings_lookup.fetch(stale))
        except SpendCapReached as exc:
            self._pause(exc)
        except Exception as exc:
            self.logger.warning(f"Ratings refresh failed: {exc}")

    def _warn_orphaned_history(self, items: list[Item]) -> None:
        """Warn about recorded history no item on the sheet claims any more.

        The item name is the only identity there is, so editing one on the
        Items tab does not rename its history — it starts the item over with
        no min, no mean and no drop detection until it has swept
        ``min_readings`` times again, while the old rows sit there keyed to a
        name nothing references. That is indistinguishable from a deletion, so
        it cannot be reconciled automatically; it can only be pointed out.
        """
        orphans = sorted(self.history.known_items() - {item.name for item in items})
        if orphans:
            self.logger.warning(
                f"{len(orphans)} item(s) have history but are no longer on the "
                f"Items tab, so their statistics have restarted: "
                f"{', '.join(repr(name) for name in orphans)}. If you renamed "
                f"one, rename it back or clear its old rows from the history."
            )

    def _due_for_refresh(self, items: list[Item]) -> list[Item]:
        """Items never checked, or last checked over ``refresh_rate_h`` ago."""
        last_checked = self.history.last_checked()
        cutoff = datetime.now() - timedelta(hours=self.ctrl.refresh_rate_h)
        return [
            item
            for item in items
            if (seen := last_checked.get(item.name)) is None or seen <= cutoff
        ]

    def poll(self) -> None:
        """Price only items added to the sheet since the last check."""
        if not self._gsheet.is_modified():
            return
        items = self.items_tab.read()
        known = self.history.known_items()
        new_items = [i for i in items if i.name not in known]
        if not new_items:
            return
        self.logger.info(f"Found {len(new_items)} new items")
        self._price_and_record("poll", new_items)

    def _price_and_record(self, label: str, items: list[Item]) -> None:
        """Price ``items`` and write them back, then send a completion notice.

        Only checks with work to do are announced — an idle poll would
        otherwise send a completion notice every ``poll_rate_m`` minutes.
        The exception is re-raised after notifying so ``_safe_run`` still logs
        it and the loop still retries next tick.
        """
        try:
            # Read before the append, so a new low price is compared against the
            # mean of the readings that came before it rather than one it has
            # already pulled down.
            baseline = self.history.price_stats()
            readings = self._price_all(items)
            self.history.append(readings)
            self.items_tab.write_summary(
                self.items_tab.read(),
                self.history.summarise(self.ctrl.best_window_days),
            )
        except Exception as exc:
            self.notifier.notify(format_check_failed(label, exc))
            raise
        # A paused check already reported itself once, via _pause's warning.
        if not self.paused_until:
            self.notifier.notify(format_check_complete(label, readings))
        self._notify_price_drops(readings, baseline)

    def _notify_price_drops(
        self, readings: list[PriceReading], baseline: dict[str, PriceStats]
    ) -> None:
        """Announce each item whose cheapest price has just dropped a band.

        Only the cheapest series has a mean worth comparing against, so a sweep
        with no cheapest detection produced nothing to compare. Whether this
        reads as a fresh crossing or an ongoing slide turns on where the
        *previous* reading sat, not how deep this one landed — a single sharp
        fall is still a first drop.
        """
        for reading in readings:
            stats = baseline.get(reading.item)
            if stats is None or reading.cheapest is None:
                continue
            if not stats.new_drop_band(
                reading.cheapest.price,
                self.ctrl.drop_step,
                self.ctrl.min_readings,
            ):
                continue
            if (
                reading.confidence is None
                or reading.confidence < self.ctrl.min_confidence
            ):
                # Logged, not silent: an item that is gated every run is a
                # prompt problem worth seeing, not a quiet nothing.
                confidence = (
                    "none"
                    if reading.confidence is None
                    else f"{reading.confidence:.2f}"
                )
                self.logger.info(
                    f"Drop for '{reading.item}' suppressed: confidence {confidence}"
                )
                continue
            self.notifier.notify(
                format_price_drop(
                    reading, stats.mean, still_falling=stats.last < stats.mean
                )
            )

    def _price_all(self, items: list[Item]) -> list[PriceReading]:
        last_prices = self.history.last_prices()
        blocked = self.control_tab.blocked()
        ratings = self._read_ratings()
        source_high = (
            self.history.recent_source_high(self.ctrl.best_window_days)
            if self.ctrl.resweep_thin
            else {}
        )
        readings: list[PriceReading] = []
        # Accumulated across every item: MarketSearcher.seen_sites resets on
        # each price_market() call (by design, so a failed sweep can't leak a
        # previous item's sites), so it must be collected right after each
        # sweep rather than read once after the loop — reading it once would
        # silently keep only the last item's retailers.
        seen: set[str] = set()
        try:
            for index, item in enumerate(items):
                if index and self.ctrl.request_delay_s:
                    self.stop_event.wait(self.ctrl.request_delay_s)
                last = last_prices.get(item.name)
                reading = self._sweep_one(item, last, blocked, ratings, seen)
                readings.append(
                    self._resweep_if_thin(
                        reading, item, last, blocked, source_high, ratings, seen
                    )
                )
        except SpendCapReached as exc:
            # Stop, but keep the readings already taken: they were paid for.
            self._pause(exc)
        self._register_seen(seen)
        return readings

    def _read_ratings(self) -> dict[str, Rating]:
        """The cached ratings for this sweep, or none if the cache is unreadable.

        An unreadable cache is not this sweep's problem to raise about: it
        must price items with no ratings rather than not price them at all.
        """
        try:
            return self.ratings_tab.read() or {}
        except Exception as exc:
            self.logger.warning(f"Could not read cached ratings: {exc}")
            return {}

    def _register_seen(self, seen: set[str]) -> None:
        """Add retailers this run's sweeps saw to the ratings cache.

        Only the three winning detections reach the history, so a retailer
        that competed and lost would otherwise never be known about — and
        could never become best-rated. Skipped on an empty set: register()
        reads the sheet to check for unseen sites, and a run that found
        nothing new should not spend that read.
        """
        if not seen:
            return
        try:
            self.ratings_tab.register(seen)
        except Exception as exc:
            self.logger.warning(f"Could not register retailers: {exc}")

    def _resweep_if_thin(
        self,
        reading: PriceReading,
        item: Item,
        last_price: float | None,
        blocked: set[str],
        source_high: dict[str, int],
        ratings: dict[str, Rating],
        seen: set[str],
    ) -> PriceReading:
        """Sweep once more when a result is thin for this item, and keep the better.

        Judged against the item's own recent best rather than a fixed bar, so a
        product genuinely sold by one shop never retries. Only the better
        reading is recorded: appending both would double that item's history
        and drag its mean toward a result we already believe is wrong.
        """
        high = source_high.get(item.name)
        if reading.sources is None or high is None or reading.sources >= high:
            return reading

        self.logger.info(
            f"'{item.name}' found {reading.sources} retailer(s) against a recent "
            f"best of {high}; sweeping once more."
        )
        if self.ctrl.request_delay_s:
            self.stop_event.wait(self.ctrl.request_delay_s)
        retry = self._sweep_one(item, last_price, blocked, ratings, seen)
        if (retry.sources or 0) <= reading.sources:
            return reading
        note = f"{retry.note}; retried after a thin sweep found {reading.sources}"
        return replace(retry, note=note.lstrip("; "))

    def _sweep_one(
        self,
        item: Item,
        last_price: float | None,
        blocked: set[str],
        ratings: dict[str, Rating],
        seen: set[str],
    ) -> PriceReading:
        timestamp = datetime.now().replace(microsecond=0)
        try:
            reading = self._searcher.price_market(
                item, last_price, timestamp, blocked, ratings
            )
        except SpendCapReached:
            raise
        except Exception as exc:
            # One failed sweep must never abort the rest of the run.
            self.logger.warning(f"Sweep for '{item.name}' failed: {exc}")
            reading = PriceReading(
                timestamp=timestamp,
                item=item.name,
                currency=self.ctrl.currency,
                status=PriceStatus.ERROR,
                note=str(exc),
            )
        # Read immediately: the next call to price_market (the next item, or
        # this item's own resweep retry) resets it before this caller gets
        # another chance to look.
        seen |= getattr(self._searcher, "seen_sites", set())
        return reading

    def _pause(self, exc: SpendCapReached) -> None:
        """Stop all checks until the spend cap resets next month.

        Resumes at noon on the 1st rather than midnight: the cap is assumed to
        reset at midnight US Pacific time, which is 08:00 in the UK.
        """
        now = datetime.now()
        self.paused_until = (now.replace(day=1) + timedelta(days=32)).replace(
            day=1, hour=12, minute=0, second=0, microsecond=0
        )
        self.logger.warning(
            f"Monthly spending cap reached; pausing until "
            f"{self.paused_until:%Y-%m-%d %H:%M}: {exc}"
        )

    def run(self) -> None:
        """Timer loop: a slow full refresh, a faster poll for new items."""
        refresh_interval_s = self.ctrl.refresh_rate_h * 3600
        poll_interval_s = self.ctrl.poll_rate_m * 60

        last_refresh = 0.0  # zero so a full refresh fires immediately on start
        last_poll = time.monotonic()

        while not self.stop_event.is_set():
            if self.paused_until:
                if datetime.now() < self.paused_until:
                    self.stop_event.wait(_MAX_SLEEP_S)
                    continue
                self.logger.info("Spend cap pause over; resuming")
                self.paused_until = None
                last_refresh = 0.0

            if (
                last_refresh == 0.0
                or time.monotonic() - last_refresh >= refresh_interval_s
            ):
                self._safe_run("Update", self.update)
                last_refresh = time.monotonic()

            if time.monotonic() - last_poll >= poll_interval_s:
                self._safe_run("Poll", self.poll)
                last_poll = time.monotonic()

            next_refresh_in = refresh_interval_s - (time.monotonic() - last_refresh)
            next_poll_in = poll_interval_s - (time.monotonic() - last_poll)
            self.stop_event.wait(
                max(0.0, min(next_refresh_in, next_poll_in, _MAX_SLEEP_S))
            )

    def _safe_run(self, label: str, action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as exc:
            # A bad run (e.g. the sheet unreachable) must not kill the
            # controller thread — it logs and the loop tries again next tick.
            self.logger.warning(f"{label} failed (will retry): {exc}")

    def start(self) -> None:
        self.thread.start()
        self.logger.info("Start thread")
        self.logger.info("PriceMonitor v%s started", __version__)
        self.notifier.notify(format_startup_message(__version__))

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join()
        self.logger.info("Stop thread")
        if self.msg_client is not None:
            detach_msg_server(self.root_logger, self.msg_client)
            self.msg_client = None


def args_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Track item prices from a Google Sheet using a search-grounded LLM"
    )
    parser.add_argument(
        "--secrets",
        type=Path,
        required=True,
        help="Directory holding config.json and .env",
    )
    parser.add_argument(
        "--msg-server",
        action="store_true",
        help=(
            "Send status messages and auth links to MsgServer, and forward "
            "warnings and errors there as well as to the console"
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single update and exit instead of starting the poll loop",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-price every item even if checked within refresh_rate_h. "
            "Each lookup costs a billed search."
        ),
    )
    return parser


def _install_sigterm_handler(monitor: PriceMonitor, logger: logging.Logger) -> None:
    """Stop the monitor cleanly when a service manager terminates the process.

    Python's default SIGTERM action kills the process outright, so ``stop()``
    would never run and the worker thread could die mid-write — leaving a
    partially rewritten history tab, since :meth:`HistoryTab.append` rewrites
    the whole frame. Setting the stop event instead lets the current lookup
    finish and the loop exit through its normal path.
    """

    def handle(signum: int, _frame: object) -> None:
        logger.info(f"Caught signal {signum}, stopping")
        monitor.stop_event.set()

    signal.signal(signal.SIGTERM, handle)


def main() -> None:
    logger = consoleLogger("main")
    args = args_parser().parse_args()
    monitor = None
    try:
        monitor = PriceMonitor(
            secrets=args.secrets.expanduser(),
            logger=logger,
            msg_server=args.msg_server,
        )
        if args.once:
            monitor.update(force=args.force)
            return
        _install_sigterm_handler(monitor, logger)
        monitor.start()
        monitor.thread.join()
    except KeyboardInterrupt:
        print("Caught Ctrl+C! Exiting gracefully.")
    finally:
        # Also runs for --once: the thread was never started, so stop() just
        # detaches the log forwarder and closes the MsgServer client.
        if monitor is not None:
            monitor.stop()


if __name__ == "__main__":
    main()
