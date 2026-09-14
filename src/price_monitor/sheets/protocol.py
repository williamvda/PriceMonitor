"""Structural interface shared by every tab module under ``price_monitor.sheets``.

Tab classes (:class:`~price_monitor.sheets.history_tab.HistoryTab` and its
siblings) depend only on the shape of a spreadsheet client — ``read``,
``write``, ``update`` — not on ``google_drive_api.GoogleSheetInterface``
specifically. Depending on this Protocol instead keeps the sheets package
decoupled from the infrastructure package and trivially testable with a fake
that implements the same three methods without inheriting from anything.

Also exports :func:`is_missing_tab_error`, the one error signature every tab
module treats as "safe to create" rather than "unreadable, do not touch".
"""

from typing import Protocol

import pandas as pd


class SheetInterface(Protocol):
    """Anything that can read, write, and update a named spreadsheet tab."""

    def read(self, sheet_name: str) -> pd.DataFrame: ...

    def write(self, sheet_name: str, df: pd.DataFrame) -> None: ...

    def update(self, sheet_name: str, df: pd.DataFrame) -> None: ...


class MonitoredSheetInterface(SheetInterface, Protocol):
    """The controller's wider view: a :class:`SheetInterface` that also
    reports whether the underlying file has changed since it was last
    checked. Only :mod:`price_monitor.price_monitor` needs ``is_modified``
    for its poll cycle — ``ItemsTab`` and ``HistoryTab`` never call it, so it
    stays off the shared ``SheetInterface`` rather than forcing every
    tab-module fake to implement a method it does not use.
    """

    def is_modified(self) -> bool: ...


def is_missing_tab_error(exc: Exception) -> bool:
    """True when ``exc`` is the Sheets API's shape for "this tab does not exist".

    A missing tab has no dedicated exception type — the API reports it as a
    failure to parse the address range, so a message-substring match is the
    only signal available. Anything else (a 5xx, a timeout, an auth failure,
    a local pandas error over ragged rows) must not match: those mean the tab
    exists and either the read or the world briefly failed, not that the tab
    is absent.
    """
    return "unable to parse range" in str(exc).lower()
