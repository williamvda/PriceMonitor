"""Unit tests for inbound MsgServer command parsing and dispatch."""

import logging
from types import SimpleNamespace

import pytest

from price_monitor.msgserver_client.msg_commands import (
    Command,
    parse_command,
    subscribe_commands,
)


class FakeClient:
    """Records the subscription so tests can drive the callback directly."""

    def __init__(self, subscribe_error: Exception | None = None) -> None:
        self.callbacks: dict[str, object] = {}
        self._subscribe_error = subscribe_error

    def subscribe(self, handle: str, callback) -> None:
        if self._subscribe_error is not None:
            raise self._subscribe_error
        self.callbacks[handle] = callback


class Calls:
    """Stands in for the monitor's handlers, recording what was asked of it."""

    def __init__(self) -> None:
        self.updates = 0
        self.pauses = 0
        self.cadences: list[float] = []
        self.replies: list[str] = []

    def update(self) -> None:
        self.updates += 1

    def pause(self) -> str:
        self.pauses += 1
        return "paused"


def _send(text: str) -> Calls:
    client = FakeClient()
    calls = Calls()
    subscribe_commands(
        client,
        handle="pm",
        on_update=calls.update,
        on_pause=calls.pause,
        on_cadence=calls.cadences.append,
        notify=calls.replies.append,
        logger=logging.getLogger("test-commands"),
    )
    client.callbacks["pm"](SimpleNamespace(text=text))
    return calls


@pytest.mark.parametrize(
    "text, command",
    [
        ("update", Command.UPDATE),
        (" Pause ", Command.PAUSE),
        ("cadence 12", Command.CADENCE),
        ("help", Command.HELP),
        ("cadence", Command.UNKNOWN),
        ("update now", Command.UNKNOWN),
        ("🟢 PriceMonitor v0.1.7 started", Command.UNKNOWN),
    ],
)
def test_parse_command(text, command):
    assert parse_command(text) is command


def test_update_queues_an_update_and_says_so():
    calls = _send("update")
    assert calls.updates == 1
    assert calls.replies == ["⏳ Update queued"]


def test_pause_replies_with_the_handlers_message():
    calls = _send("pause")
    assert calls.pauses == 1
    assert calls.replies == ["paused"]


def test_cadence_sets_the_hours():
    calls = _send("cadence 1.5")
    assert calls.cadences == [1.5]
    assert calls.replies == ["🕒 Updating every 1.5h"]


@pytest.mark.parametrize("arg", ["0", "-3", "soon", "nan", "inf"])
def test_a_bad_cadence_is_refused(arg):
    calls = _send(f"cadence {arg}")
    assert calls.cadences == []
    assert calls.replies == [f"❌ Cadence must be a number of hours, got '{arg}'"]


def test_unknown_text_lists_the_commands():
    calls = _send("bananas")
    assert calls.updates == calls.pauses == 0
    assert calls.replies[0].startswith("unknown command 'bananas'")
    assert "cadence <hours>" in calls.replies[0]


def test_an_unreachable_server_does_not_raise():
    subscribed = subscribe_commands(
        FakeClient(subscribe_error=TimeoutError("no server")),
        handle="pm",
        on_update=lambda: None,
        on_pause=lambda: "",
        on_cadence=lambda _: None,
        notify=lambda _: None,
        logger=logging.getLogger("test-commands"),
    )
    assert subscribed is False
