"""Structural interface shared by every tab module under ``price_monitor.sheets``.

Tab classes (:class:`~price_monitor.sheets.history_tab.HistoryTab` and its
siblings) depend only on the shape of a spreadsheet client — ``read``,
``write``, ``update`` — not on ``google_drive_api.GoogleSheetInterface``
specifically. Depending on this Protocol instead keeps the sheets package
decoupled from the infrastructure package and trivially testable with a fake
that implements the same three methods without inheriting from anything.
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
