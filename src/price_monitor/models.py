"""Value objects for PriceMonitor.

Defines the tracked :class:`Item`, the :class:`PriceReading` recorded for it on
each run, and the :class:`PriceStatus` describing how that reading turned out.
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
