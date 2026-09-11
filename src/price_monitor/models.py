"""Value objects for PriceMonitor.

Defines the tracked :class:`Item`, the :class:`Detection` naming one retailer's
offer, and the :class:`PriceReading` recorded for it on each run, the
:class:`PriceStatus` describing how that reading turned out, and the
:class:`PriceStats` summarising an item's history so far. Exports the
:func:`supplier_domain` utility that renders a candidate's URL as a site.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from urllib.parse import urlparse


def supplier_domain(url: str) -> str:
    """The bare domain of ``url``, as a :class:`Detection`'s ``site`` value."""
    netloc = urlparse(url.strip()).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


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
class Detection:
    """One retailer's offer, as one of a sweep's three answers.

    ``rating`` is the display string the sheet shows, already formatted — it is
    never parsed back into a number. Ranking happens on :class:`Candidate`
    before a detection is built.
    """

    price: float
    site: str
    rating: str = ""


@dataclass(frozen=True)
class Item:
    """One tracked product. The user types only its name."""

    name: str


@dataclass(frozen=True)
class PriceReading:
    """One sweep's result for one item.

    A detection is ``None`` when the sweep could not produce it: no rated
    candidate for ``best_rated``, no surviving candidate at all for any of them.
    """

    timestamp: datetime
    item: str
    currency: str
    status: PriceStatus
    best_rated: Detection | None = None
    cheapest: Detection | None = None
    best_value: Detection | None = None
    confidence: float | None = None
    sources: int | None = None
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
