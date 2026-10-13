"""Inbound commands received from MsgServer.

Public surface: :func:`parse_command` for turning message text into a
:class:`Command`, :func:`format_help_message` for the reply listing them, and
:func:`subscribe_commands` to wire a client's subscription to the monitor's
handlers.
"""

import logging
from enum import Enum
from typing import Any, Callable


class Command(str, Enum):
    UPDATE = "update"
    PAUSE = "pause"
    CADENCE = "cadence"
    HELP = "help"
    UNKNOWN = "unknown"


def parse_command(text: str) -> Command:
    """Classify inbound message text. Unrecognised text is never an update.

    ``cadence`` takes exactly one argument, the hours; every other command
    takes none.
    """
    words = text.strip().lower().split()
    if len(words) == 2 and words[0] == Command.CADENCE.value:
        return Command.CADENCE
    if len(words) == 1 and words[0] in (
        Command.UPDATE.value,
        Command.PAUSE.value,
        Command.HELP.value,
    ):
        return Command(words[0])
    return Command.UNKNOWN


def format_help_message() -> str:
    """Return the list of commands this handle answers to."""
    return (
        "update - re-price every item now, ending any pause\n"
        "-----\n"
        "pause - stop all checks until next month\n"
        "-----\n"
        "cadence <hours> - set the hours between full updates\n"
        "-----\n"
        "help - return a list of valid commands"
    )


def subscribe_commands(
    client: Any,
    handle: str,
    on_update: Callable[[], None],
    on_pause: Callable[[], str],
    on_cadence: Callable[[float], None],
    notify: Callable[[str], None],
    logger: logging.Logger,
) -> bool:
    """Subscribe to ``handle`` and dispatch inbound commands.

    Returns True once subscribed. An unreachable server surfaces as a
    TimeoutError from ``subscribe()``; that is logged and reported as False
    rather than raised, so it cannot stop the monitor from starting.

    The handlers run on the client's receive thread, so they must only hand
    work to the monitor's thread — never price anything inline. ``on_pause``
    returns the reply describing how long the pause lasts.
    """

    def _dispatch(message: Any) -> None:
        command = parse_command(message.text)
        logger.debug(f"📥 Command received: {command.value}")
        try:
            if command is Command.UPDATE:
                on_update()
                notify("⏳ Update queued")
            elif command is Command.PAUSE:
                notify(on_pause())
            elif command is Command.CADENCE:
                arg = message.text.split()[1]
                try:
                    hours = float(arg)
                except ValueError:
                    hours = 0.0
                # Also rejects nan and inf, which float() accepts.
                if not 0 < hours < 24 * 366:
                    notify(f"❌ Cadence must be a number of hours, got '{arg}'")
                    return
                on_cadence(hours)
                notify(f"🕒 Updating every {hours:g}h")
            elif command is Command.HELP:
                notify(format_help_message())
            else:
                notify(
                    f"unknown command '{message.text.strip()}'\n"
                    f"{format_help_message()}"
                )
        except Exception as exc:
            # This runs on the client's receive thread; letting an exception
            # out would deafen the client for every later message.
            logger.warning(f"⚠️ Command '{message.text}' failed: {exc}")

    try:
        client.subscribe(handle, _dispatch)
    except Exception as exc:
        logger.warning(f"⚠️ Could not subscribe to '{handle}' for commands: {exc}")
        return False

    logger.info(f"📡 Listening for commands on '{handle}'")
    return True
