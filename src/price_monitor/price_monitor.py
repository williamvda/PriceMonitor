"""PriceMonitor controller and CLI entry point.

Owns timing and orchestration: a slow full refresh of every tracked item
(``refresh_rate_h``) and a faster poll that prices only items newly added to
the sheet (``poll_rate_m``). All state lives in the spreadsheet, so a restart
loses nothing. Neither JSON parsing nor sheet-range addressing belongs here —
those live in :mod:`price_monitor.app_config` and :mod:`price_monitor.sheets`.
"""

import argparse
import logging
import signal
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from google_drive_api import GoogleSheetInterface
from py_utils.logger import consoleLogger, get_child_logger

from price_monitor.app_config import PriceCtrl, load_price_config
from price_monitor.models import Item, PriceReading, PriceStatus
from price_monitor.msgserver_client.msg_forwarder import (
    MsgNotifier,
    attach_msg_server,
    detach_msg_server,
    format_check_complete,
    format_check_failed,
    format_check_started,
    format_startup_message,
)
from price_monitor.search.searcher import PriceSearcher
from price_monitor.sheets.history_tab import HistoryTab
from price_monitor.sheets.items_tab import ItemsTab
from price_monitor.sheets.protocol import MonitoredSheetInterface

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
        gsheet: MonitoredSheetInterface = GoogleSheetInterface(
            config=config.drive_config, logger=logger
        )
        self._init_parts(
            gsheet=gsheet,
            searcher=PriceSearcher(config.llm_config, config.price_ctrl, logger),
        )

    @classmethod
    def for_test(
        cls,
        gsheet: MonitoredSheetInterface,
        searcher: PriceSearcher,
        logger: logging.Logger,
    ) -> "PriceMonitor":
        """Build a controller around fakes, skipping config and network setup."""
        monitor = cls.__new__(cls)
        monitor.logger = get_child_logger(logger, cls.__name__)
        monitor.ctrl = PriceCtrl(request_delay_s=0.0)
        monitor._init_parts(gsheet=gsheet, searcher=searcher)
        monitor.root_logger = logger
        monitor.msg_client = None
        monitor.notifier = MsgNotifier(client=None, logger=monitor.logger)
        return monitor

    def _init_parts(
        self, gsheet: MonitoredSheetInterface, searcher: PriceSearcher
    ) -> None:
        self._searcher = searcher
        self.items_tab = ItemsTab(gsheet, self.ctrl.items_sheet, self.logger)
        self.history = HistoryTab(gsheet, self.ctrl.history_sheet, self.logger)
        self._gsheet = gsheet
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="PriceMonitor")

    def update(self, force: bool = False) -> None:
        """Price tracked items due for a refresh and rewrite the summary.

        An item checked less than ``refresh_rate_h`` ago is skipped: each
        lookup costs a billed search, and repeated CLI runs would otherwise
        re-price the same items for no new information. ``force`` re-prices
        everything regardless.
        """
        items = self.items_tab.read()
        if not items:
            self.logger.info("No items to price")
            return

        due = items if force else self._due_for_refresh(items)
        if skipped := len(items) - len(due):
            self.logger.info(
                f"Skipping {skipped} item(s) checked within the last "
                f"{self.ctrl.refresh_rate_h}h"
            )
        if not due:
            return

        self.logger.info(f"Pricing {len(due)} items")
        self._price_and_record("update", due)

    def _due_for_refresh(self, items: list[Item]) -> list[Item]:
        """Items never checked, or last checked over ``refresh_rate_h`` ago."""
        last_checked = self.history.last_checked()
        cutoff = datetime.now() - timedelta(hours=self.ctrl.refresh_rate_h)
        return [
            item
            for item in items
            if (seen := last_checked.get((item.name, item.website))) is None
            or seen <= cutoff
        ]

    def poll(self) -> None:
        """Price only items added to the sheet since the last check."""
        if not self._gsheet.is_modified():
            return
        items = self.items_tab.read()
        known = self.history.known_items()
        new_items = [i for i in items if (i.name, i.website) not in known]
        if not new_items:
            return
        self.logger.info(f"Found {len(new_items)} new items")
        self._price_and_record("poll", new_items)

    def _price_and_record(self, label: str, items: list[Item]) -> None:
        """Price ``items`` and write them back, bracketed by notifications.

        Only checks with work to do are announced — an idle poll would
        otherwise send a start/complete pair every ``poll_rate_m`` minutes.
        The exception is re-raised after notifying so ``_safe_run`` still logs
        it and the loop still retries next tick.
        """
        self.notifier.notify(format_check_started(label, len(items)))
        try:
            readings = self._price_all(items)
            self.history.append(readings)
            self.items_tab.write_summary(
                self.items_tab.read(), self.history.summarise()
            )
        except Exception as exc:
            self.notifier.notify(format_check_failed(label, exc))
            raise
        self.notifier.notify(format_check_complete(label, readings))

    def _price_all(self, items: list[Item]) -> list[PriceReading]:
        last_prices = self.history.last_prices()
        readings: list[PriceReading] = []

        for index, item in enumerate(items):
            if index and self.ctrl.request_delay_s:
                self.stop_event.wait(self.ctrl.request_delay_s)
            readings.append(
                self._price_one(item, last_prices.get((item.name, item.website)))
            )
        return readings

    def _price_one(self, item: Item, last_price: float | None) -> PriceReading:
        timestamp = datetime.now().replace(microsecond=0)
        try:
            return self._searcher.price(item, last_price, timestamp)
        except Exception as exc:
            # One unreachable site must never abort the rest of the run.
            self.logger.warning(f"Pricing '{item.name}' failed: {exc}")
            return PriceReading(
                timestamp=timestamp,
                item=item.name,
                website=item.website,
                price=None,
                currency=self.ctrl.currency,
                status=PriceStatus.ERROR,
                note=str(exc),
            )

    def run(self) -> None:
        """Timer loop: a slow full refresh, a faster poll for new items."""
        refresh_interval_s = self.ctrl.refresh_rate_h * 3600
        poll_interval_s = self.ctrl.poll_rate_m * 60

        last_refresh = 0.0  # zero so a full refresh fires immediately on start
        last_poll = time.monotonic()

        while not self.stop_event.is_set():
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
        self.notifier.notify(format_startup_message())

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
