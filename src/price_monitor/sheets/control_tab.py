"""User-owned Control tab.

Holds the settings that are easier to manage from the spreadsheet than from a
config file, one column per control. Today that is ``blocked`` — retailers
never to report — and the tab exists so further controls can be added beside
it without a redeploy.
"""

import logging
from typing import Callable

import pandas as pd
from py_utils.logger import get_child_logger

from price_monitor.sheets.protocol import SheetInterface

COLUMNS = ["blocked"]


class ControlTab:
    """Reads the user's controls, and never writes over them."""

    def __init__(
        self,
        gsheet: SheetInterface,
        sheet_name: str,
        logger: logging.Logger,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self.logger = get_child_logger(logger, __class__.__name__)
        self._gsheet = gsheet
        self._sheet_name = sheet_name
        self._notify = notify or (lambda _message: None)

    def blocked(self) -> set[str]:
        """Sites never to report, as bare domains or brand names."""
        frame = self._read()
        if frame is None or frame.empty or "blocked" not in frame.columns:
            return set()
        return {
            cleaned
            for value in frame["blocked"]
            if not pd.isna(value) and (cleaned := str(value).strip().lower())
        }

    def _read(self) -> pd.DataFrame | None:
        """The tab's contents, or ``None`` when it could not be read.

        The two failures are told apart by who raises. A tab that does not
        exist fails in the Sheets API, which cannot parse the range — that is
        the first run, and creating the tab makes the feature discoverable. A
        tab whose every row is short of its header fails in pandas, locally,
        which means the tab exists and still holds the user's data. Writing
        then would clear it, because ``write()`` clears before it writes.
        """
        try:
            return self._gsheet.read(sheet_name=self._sheet_name)
        except ValueError as exc:
            message = (
                f"The '{self._sheet_name}' tab could not be read ({exc}), so no "
                f"controls are being applied. Every row is probably short of "
                f"its header — a column added and later emptied does this."
            )
            self.logger.warning(message)
            self._notify(f"⚠️ PriceMonitor — {message}")
            return None
        except Exception as exc:
            self.logger.info(f"No '{self._sheet_name}' tab ({exc}); creating it.")
            self._create()
            return None

    def _create(self) -> None:
        """Add an empty Control tab. A failure here must not stop the run."""
        try:
            self._gsheet.write(
                sheet_name=self._sheet_name, df=pd.DataFrame(columns=COLUMNS)
            )
        except Exception as exc:
            self.logger.warning(f"Could not create '{self._sheet_name}': {exc}")
