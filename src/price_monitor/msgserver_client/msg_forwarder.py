"""Send log records and status notifications to a MsgServer instance.

Public surface: :class:`MsgServerHandler`, a logging handler that forwards each
warning-or-above record to a MsgServer handle; :class:`MsgNotifier`, for
deliberate status messages that are not log records; :func:`attach_msg_server`
and :func:`detach_msg_server` for wiring and teardown; and the
``format_*`` helpers that render the start, complete, failure, and
price-drop notices for one pricing check. The handler is purely additive
— records still reach every other handler on the logger, so console
output is unchanged whether or not the server is reachable.
"""

import logging
import threading
from typing import Any

from price_monitor.app_config import MsgConfig
from price_monitor.models import PriceReading, PriceStatus

_MISSING_PACKAGE_HINT = (
    "The msgserver client is not installed. "
    "Install it with: pip install 'price-monitor[msgserver]'"
)

# A reading with one of these statuses carries a usable price that was written
# to the history tab; anything else produced no number for this run.
_RECORDED_STATUSES = (PriceStatus.OK, PriceStatus.SUSPECT)


class MsgServerHandler(logging.Handler):
    """Sends each record it handles to a MsgServer handle.

    Failures are swallowed: a send that raises or goes unacknowledged must
    never break the logging call that triggered it, nor stop the record
    reaching the console.
    """

    def __init__(
        self,
        client: Any,
        handle: str = "pm",
        level: int = logging.WARNING,
    ) -> None:
        super().__init__(level=level)
        self.client = client
        # Not ``self.handle`` — that would shadow logging.Handler.handle().
        self.msg_handle = handle
        # A send that itself logs at WARNING+ would re-enter emit() on this
        # same thread and recurse without bound; the flag breaks that cycle.
        self._forwarding = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._forwarding, "active", False):
            return
        self._forwarding.active = True
        try:
            self.client.send(self.msg_handle, self.format(record))
        except Exception:
            self.handleError(record)
        finally:
            self._forwarding.active = False


class MsgNotifier:
    """Sends deliberate status messages, as opposed to forwarded log records.

    Constructed with ``None`` when ``--msg-server`` is off, which makes every
    ``notify()`` a no-op — callers do not need to guard their calls.
    """

    def __init__(
        self,
        client: Any = None,
        handle: str = "pm",
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._handle = handle
        self._logger = logger or logging.getLogger(__name__)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def notify(self, text: str) -> None:
        """Send ``text``, swallowing any failure — a status message is never
        important enough to interrupt the work that triggered it."""
        if self._client is None:
            return
        try:
            self._client.send(self._handle, text)
        except Exception as exc:
            self._logger.warning(f"⚠️ MsgServer notify failed: {exc}")


def format_startup_message(version: str) -> str:
    """Render the message sent when the monitor's poll loop starts.

    ``version`` is the installed distribution's version, which the tag gate
    ties to the deployed git tag -- so the message identifies the running
    revision, not just that something started.
    """
    return f"🟢 PriceMonitor v{version} started"


def format_check_started(label: str, item_count: int) -> str:
    """Render the notice sent when a pricing check begins."""
    return f"⏳ PriceMonitor {label} started — {item_count} item(s)"


def format_check_complete(label: str, readings: list[PriceReading]) -> str:
    """Render the outcome of one pricing check as a plain-text message.

    A reading that came back SUSPECT still produced a price, so it counts as
    priced rather than failed — the flag is on the value, not the lookup.
    """
    total = len(readings)
    priced = sum(1 for r in readings if r.status in _RECORDED_STATUSES)
    icon = "✅" if priced == total else "❌"
    return (
        f"{icon} PriceMonitor {label} complete — "
        f"{priced}/{total} priced, {total - priced} failed"
    )


def format_price_drop(
    reading: PriceReading, mean: float, still_falling: bool = False
) -> str:
    """Render the notice sent when an item drops further below its mean price.

    ``still_falling`` marks a price that was already under the mean before this
    reading, so an ongoing slide reads differently from a fresh crossing.

    A SUSPECT reading is flagged rather than suppressed: a big crash is exactly
    what is worth knowing about, but it is also the likeliest hallucination, so
    the message says which it might be and leaves the call to the reader.
    """
    change = (reading.price - mean) / mean
    trend = " still falling," if still_falling else ""
    suffix = " (suspect)" if reading.status is PriceStatus.SUSPECT else ""
    return (
        f"📉 PriceMonitor — {reading.item} at {reading.website}: "
        f"{reading.price:.2f} {reading.currency},{trend} "
        f"below its {mean:.2f} {reading.currency} mean ({change:.0%}){suffix}"
    )


def format_check_failed(label: str, error: Exception) -> str:
    """Render the notice sent when a check aborts before it could report."""
    return f"❌ PriceMonitor {label} failed — {error}"


def attach_msg_server(logger: logging.Logger, config: MsgConfig) -> Any:
    """Connect to MsgServer and tee ``logger``'s warnings and errors to it.

    Returns the connected client so the caller can close it on shutdown.
    Raises ImportError with an install hint if the optional dependency is
    missing. An unreachable server is not an error — it is logged and the
    handler stays attached so forwarding resumes if the server comes back.
    """
    try:
        from msgserver import Client
    except ImportError as exc:
        raise ImportError(_MISSING_PACKAGE_HINT) from exc

    client = Client(
        name=config.handle,
        router_endpoint=config.router_endpoint,
        sub_endpoint=config.sub_endpoint,
        timeout_ms=config.timeout_ms,
    )

    handler = MsgServerHandler(client, handle=config.handle)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(name)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)

    if not client.ping():
        logger.info(f"MsgServer unreachable at {config.router_endpoint}")
    else:
        logger.info(f"MsgServer forwarding enabled on handle '{config.handle}'")

    return client


def detach_msg_server(logger: logging.Logger, client: Any) -> None:
    """Stop forwarding and close the client. Safe to call with ``None``.

    The handler comes off before the client closes — the other order leaves a
    window where a shutdown log record would be sent over a closed socket.
    """
    for handler in [h for h in logger.handlers if isinstance(h, MsgServerHandler)]:
        logger.removeHandler(handler)
        handler.close()

    if client is not None:
        client.close()
