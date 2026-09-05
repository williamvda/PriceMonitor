"""Tests for the MsgServer log forwarder, notifier, and message formatting."""

import logging
import logging.handlers
from datetime import datetime

import pytest

from price_monitor.app_config import MsgConfig
from price_monitor.models import PriceReading, PriceStatus
from price_monitor.msgserver_client import msg_forwarder
from price_monitor.msgserver_client.msg_forwarder import (
    MsgNotifier,
    MsgServerHandler,
    attach_msg_server,
    detach_msg_server,
    format_check_complete,
    format_check_failed,
    format_check_started,
    format_price_drop,
    format_startup_message,
)


class FakeClient:
    """Stands in for msgserver.Client: records sends, never touches a socket."""

    def __init__(self, reachable: bool = True, raise_on_send: bool = False) -> None:
        self.sends: list[tuple[str, str]] = []
        self.reachable = reachable
        self.raise_on_send = raise_on_send
        self.closed = False

    def send(self, handle: str, text: str) -> None:
        if self.raise_on_send:
            raise ConnectionError("server gone")
        self.sends.append((handle, text))

    def ping(self) -> bool:
        return self.reachable

    def close(self) -> None:
        self.closed = True


def _reading(status: PriceStatus) -> PriceReading:
    return PriceReading(
        timestamp=datetime(2026, 8, 26, 6, 0, 0),
        item="Widget",
        website="shop.example",
        price=100.0 if status is not PriceStatus.ERROR else None,
        currency="GBP",
        status=status,
    )


@pytest.fixture
def logger() -> logging.Logger:
    log = logging.getLogger("test_msg_forwarder")
    log.handlers.clear()
    log.setLevel(logging.DEBUG)
    log.propagate = False
    yield log
    log.handlers.clear()


# --- message formatting ----------------------------------------------------


def test_startup_message_names_the_monitor():
    assert format_startup_message("0.1.7") == "🟢 PriceMonitor v0.1.7 started"


def test_check_started_reports_the_label_and_count():
    assert format_check_started("update", 3) == (
        "⏳ PriceMonitor update started — 3 item(s)"
    )


def test_all_priced_reports_success():
    readings = [_reading(PriceStatus.OK)] * 3
    message = format_check_complete("update", readings)
    assert message == "✅ PriceMonitor update complete — 3/3 priced, 0 failed"


def test_failures_are_counted_not_ratioed():
    readings = [
        _reading(PriceStatus.OK),
        _reading(PriceStatus.ERROR),
        _reading(PriceStatus.NOT_FOUND),
    ]
    message = format_check_complete("update", readings)
    assert message == "❌ PriceMonitor update complete — 1/3 priced, 2 failed"


def test_suspect_counts_as_priced():
    readings = [_reading(PriceStatus.OK), _reading(PriceStatus.SUSPECT)]
    message = format_check_complete("update", readings)
    assert message == "✅ PriceMonitor update complete — 2/2 priced, 0 failed"


def test_empty_readings_do_not_divide_by_zero():
    assert format_check_complete("poll", []) == (
        "✅ PriceMonitor poll complete — 0/0 priced, 0 failed"
    )


def test_check_failed_carries_the_error_text():
    message = format_check_failed("update", RuntimeError("sheet unreachable"))
    assert message == "❌ PriceMonitor update failed — sheet unreachable"


# --- MsgServerHandler ------------------------------------------------------


def test_warnings_are_forwarded_and_info_is_not(logger):
    client = FakeClient()
    logger.addHandler(MsgServerHandler(client, handle="pm"))

    logger.info("routine")
    logger.warning("something odd")

    assert [handle for handle, _ in client.sends] == ["pm"]
    assert "something odd" in client.sends[0][1]


def test_a_failing_send_does_not_break_the_logging_call(logger):
    console = logging.handlers.MemoryHandler(capacity=10)
    logger.addHandler(console)
    handler = MsgServerHandler(FakeClient(raise_on_send=True), handle="pm")
    handler.handleError = lambda record: None  # silence the stderr traceback
    logger.addHandler(handler)

    logger.warning("still logged")

    assert [r.getMessage() for r in console.buffer] == ["still logged"]


def test_a_client_that_logs_while_sending_does_not_recurse(logger):
    """A send that itself warns would re-enter emit(); the guard must stop it."""
    calls: list[str] = []

    class ChattyClient(FakeClient):
        def send(self, handle: str, text: str) -> None:
            calls.append(text)
            logger.warning("send is slow")

    logger.addHandler(MsgServerHandler(ChattyClient(), handle="pm"))
    logger.warning("original")

    assert len(calls) == 1


# --- MsgNotifier -----------------------------------------------------------


def test_notify_is_a_no_op_without_a_client():
    notifier = MsgNotifier(client=None)
    assert notifier.enabled is False
    notifier.notify("ignored")  # must not raise


def test_notify_sends_on_the_configured_handle():
    client = FakeClient()
    MsgNotifier(client, handle="pm").notify("hello")
    assert client.sends == [("pm", "hello")]


def test_notify_swallows_send_failures(logger):
    records = logging.handlers.MemoryHandler(capacity=10)
    logger.addHandler(records)

    MsgNotifier(FakeClient(raise_on_send=True), logger=logger).notify("hello")

    assert any("notify failed" in r.getMessage() for r in records.buffer)


# --- attach / detach -------------------------------------------------------


def test_attach_raises_with_an_install_hint_when_the_package_is_missing(
    logger, monkeypatch
):
    monkeypatch.setitem(__import__("sys").modules, "msgserver", None)
    with pytest.raises(ImportError) as excinfo:
        attach_msg_server(logger, MsgConfig())
    assert "price-monitor[msgserver]" in str(excinfo.value)


def _patch_client(monkeypatch, client: FakeClient) -> None:
    """Install a fake ``msgserver`` module so attach_msg_server can import it."""
    import sys
    import types

    module = types.ModuleType("msgserver")
    module.Client = lambda **kwargs: client
    monkeypatch.setitem(sys.modules, "msgserver", module)


def test_attach_keeps_forwarding_when_the_server_is_unreachable(logger, monkeypatch):
    client = FakeClient(reachable=False)
    _patch_client(monkeypatch, client)

    returned = attach_msg_server(logger, MsgConfig())
    logger.warning("later warning")

    assert returned is client
    assert any("later warning" in text for _, text in client.sends)


def test_detach_removes_the_handler_before_closing_the_client(logger, monkeypatch):
    client = FakeClient()
    _patch_client(monkeypatch, client)
    attach_msg_server(logger, MsgConfig())

    detach_msg_server(logger, client)
    logger.warning("after detach")

    assert client.closed is True
    assert not any("after detach" in text for _, text in client.sends)
    assert not any(isinstance(h, MsgServerHandler) for h in logger.handlers)


def test_detach_is_safe_without_a_client(logger):
    detach_msg_server(logger, None)  # must not raise


def test_handler_default_handle_matches_the_config_default():
    assert MsgServerHandler(FakeClient()).msg_handle == MsgConfig().handle
    assert msg_forwarder.MsgNotifier(FakeClient())._handle == MsgConfig().handle


def test_price_drop_names_the_item_and_both_prices():
    message = format_price_drop(_reading(PriceStatus.OK), mean=125.0)
    assert message == (
        "📉 PriceMonitor — Widget at shop.example: 100.00 GBP, "
        "below its 125.00 GBP mean (-20%)"
    )


def test_a_suspect_drop_is_flagged_so_it_can_be_taken_with_a_pinch_of_salt():
    message = format_price_drop(_reading(PriceStatus.SUSPECT), mean=125.0)
    assert message.endswith("(-20%) (suspect)")


def test_a_further_fall_is_marked_as_still_falling():
    message = format_price_drop(
        _reading(PriceStatus.OK), mean=125.0, still_falling=True
    )
    assert message == (
        "📉 PriceMonitor — Widget at shop.example: 100.00 GBP, still falling, "
        "below its 125.00 GBP mean (-20%)"
    )
