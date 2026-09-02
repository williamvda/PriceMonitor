"""Value objects for PriceMonitor.

Defines the tracked :class:`Item`, the :class:`PriceReading` recorded for it on
each run, the :class:`PriceStatus` describing how that reading turned out, and
the :class:`PriceStats` summarising an item's history so far.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class PriceStatus(str, Enum):
    """Outcome of a single price lookup."""

    OK = "ok"
    SUSPECT = "suspect"
    NOT_FOUND = "not_found"
    WRONG_CURRENCY = "wrong_currency"
    REJECTED = "rejected"
    PARSE_ERROR = "parse_error"
    ERROR = "error"


@dataclass(frozen=True)
class Item:
    """One tracked product, as written on the Items tab."""

    name: str
    website: str

    @property
    def is_direct_url(self) -> bool:
        """True when ``website`` names a product page rather than a site to search."""
        target = self.website.strip()
        for scheme in ("https://", "http://"):
            if target.lower().startswith(scheme):
                target = target[len(scheme) :]
                break
        _, _, remainder = target.partition("/")
        return bool(remainder.strip())


@dataclass(frozen=True)
class PriceReading:
    """A single observation of an item's price at a point in time."""

    timestamp: datetime
    item: str
    website: str
    price: float | None
    currency: str
    status: PriceStatus
    source_url: str = ""
    note: str = ""


# Below this many readings the mean tracks jitter rather than a settled price,
# so a "drop" against it says nothing useful.
_MIN_READINGS = 3


@dataclass(frozen=True)
class PriceStats:
    """An item's priced history summarised, as the baseline for the next reading."""

    mean: float
    last: float
    count: int

    def new_drop_band(self, price: float, step: float) -> int:
        """The deeper band ``price`` has just entered, or 0 if it has not.

        Bands sit at the mean and every ``step`` below it, so band 1 is the
        first crossing below the mean at any depth and each further band is
        another ``step`` given up. A reading only reports when it lands deeper
        than the one before it: a price sitting still holds its band and stays
        quiet, while a slow slide opens a new band every few refreshes. Both
        prices are measured against the same mean — the one from before this
        reading was recorded.
        """
        if self.count < _MIN_READINGS:
            return 0
        band = self._band(price, step)
        return band if band > self._band(self.last, step) else 0

    def _band(self, price: float, step: float) -> int:
        if price >= self.mean:
            return 0
        return 1 + int((self.mean - price) / (self.mean * step))
