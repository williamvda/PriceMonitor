"""Inbound commands received from MsgServer.

Public surface: :func:`parse_command` for turning message text into a
:class:`Command`, :func:`format_help_message` and
:func:`unknown_command_message` for the replies, and
:func:`subscribe_commands` to wire a client's subscription to a handler.

The command vocabulary lives here rather than in MsgServer: handles are the
server's concern, but which commands a handle answers to belongs to the client
that owns it.
"""

import logging
from enum import Enum
from typing import Any, Callable

# Every spelling that means "run an update now".
_PROCESS_ALIASES = ("process", "update", "refresh")


class Command(str, Enum):
    PROCESS = "process"
    HELP = "help"
    UNKNOWN = "unknown"


def parse_command(text: str) -> Command:
    """Classify inbound message text. Unrecognised text is never an update."""
    word = text.strip().lower()
    if word in _PROCESS_ALIASES:
        return Command.PROCESS
    if word == Command.HELP.value:
        return Command.HELP
    return Command.UNKNOWN


def format_help_message() -> str:
    """Return the list of commands this handle answers to."""
    return (
        f"{'/'.join(_PROCESS_ALIASES)} - run the update immediately\n"
        "-----\n"
        "help - return a list of valid commands"
    )


def unknown_command_message(text: str) -> str:
    """Return the reply for text that matched no command."""
    return f"unknown command '{text.strip()}'\n{format_help_message()}"


def subscribe_commands(
    client: Any,
    handle: str,
    on_process: Callable[[], None],
    notify: Callable[[str], None] | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    """Subscribe to ``handle`` and dispatch inbound commands.

    Returns True once subscribed. An unreachable server surfaces as a
    TimeoutError from ``subscribe()``; that is logged and reported as False
    rather than raised, so it cannot stop the monitor from starting.

    ``on_process`` runs on the client's receive thread, so it must only hand
    work to another thread — never block or touch the client's sockets.
    """
    log = logger or logging.getLogger(__name__)
    reply = notify or (lambda _: None)

    def _dispatch(message: Any) -> None:
        command = parse_command(message.text)
        log.debug(f"📥 Command received: {command.value}")
        try:
            if command is Command.PROCESS:
                on_process()
                reply("⏳ Update queued")
            elif command is Command.HELP:
                reply(format_help_message())
            else:
                reply(unknown_command_message(message.text))
        except Exception as e:
            # This runs on the client's receive thread; letting an exception
            # out would deafen the client for every later message.
            log.warning(f"⚠️ Command '{message.text}' failed: {e}")

    try:
        client.subscribe(handle, _dispatch)
    except Exception as e:
        log.warning(f"⚠️ Could not subscribe to '{handle}' for commands: {e}")
        return False

    log.info(f"📡 Listening for commands on '{handle}'")
    return True
