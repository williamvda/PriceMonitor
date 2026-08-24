"""Send log records and status notifications to a MsgServer instance.

Public surface: :class:`MsgServerHandler`, a logging handler that forwards each
warning-or-above record to a MsgServer handle; :class:`MsgNotifier`, for
deliberate status messages that are not log records; :func:`attach_msg_server`
and :func:`detach_msg_server` for wiring and teardown; and
:func:`format_update_summary` for rendering per-bank update outcomes. The
handler is purely additive — records still reach every other handler on the
logger, so console output is unchanged whether or not the server is reachable.
"""

import logging
import threading
from typing import Any

from price_monitor.models import Item, PriceReading, PriceStatus
from price_monitor.app_config import MsgConfig

_MISSING_PACKAGE_HINT = (
    "The msgserver client is not installed. "
    "Install it with: pip install 'finance-monitor[msgserver]'"
)


class MsgServerHandler(logging.Handler):
    """Sends each record it handles to a MsgServer handle.

    Failures are swallowed: a send that raises or goes unacknowledged must
    never break the logging call that triggered it, nor stop the record
    reaching the console.
    """

    def __init__(
        self,
        client: Any,
        handle: str = "fin_man",
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


def format_startup_message() -> str:
    """Render the message sent when the monitor starts."""
    return (
        f"🟢 PriceMonitor started"
    )


def format_update_summary(stage_results: list[PriceReading]) -> str:
    """Render per-bank outcomes for one update as a plain-text message.

    A stage missing from ``stage_results`` did not run — most often
    ``transactions``, which is skipped when no balance changed.
    """
    total = len(stage_results)
    success = sum(1 for r in stage_results if r.status == PriceStatus.OK)
    lines = [f"{'✅' if success == total else '❌'} PriceMonitor update complete"]
    lines.append(f"[{success}/{total}] successful,")
    lines.append(f"{(total - success)/total} failed.")
    return "\n".join(lines)


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
        logger.info("MsgServer unreachable at %s", config.router_endpoint)
    else:
        logger.info("MsgServer forwarding enabled on handle '%s'", config.handle)

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
