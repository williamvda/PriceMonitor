"""Confidence scoring for a market sweep.

Grades a set of retailer candidates on how well it supports the claim "this is
the best UK price" — not on whether the winning number was read correctly. Pure:
no clients, no I/O, and no reliance on the model rating its own work, which is
the part of an LLM's output least worth trusting.
"""

from dataclasses import dataclass
from statistics import median

# A rating on fewer reviews than this is noise, and reads as unknown.
MIN_RATING_COUNT = 50
# Below this, a retailer is dropped by market.py before scoring ever happens.
FLOOR_RATING = 3.0

_TARGET_SOURCES = 3
# The outlier factor is full above _OUTLIER_FULL and zero at or below
# _OUTLIER_ZERO, sliding linearly between the two.
_OUTLIER_FULL = 0.8
_OUTLIER_ZERO = 0.4
_RATING_FULL = 4.0
_RATING_FLOOR_SCORE = 0.3
_UNKNOWN_RATING_SCORE = 0.8


@dataclass(frozen=True)
class Candidate:
    """One retailer's offer, already validated and VAT-normalised."""

    supplier: str
    price: float
    url: str
    in_stock: bool
    rating: float | None = None
    rating_count: int | None = None
    rating_source: str = ""

    @property
    def rated(self) -> bool:
        """True when the rating rests on enough reviews to mean anything."""
        return (
            self.rating is not None
            and self.rating_count is not None
            and self.rating_count >= MIN_RATING_COUNT
        )

    @property
    def rating_label(self) -> str:
        """The sheet's display string, e.g. ``4.1 Trustpilot``; empty if unrated."""
        if not self.rated:
            return ""
        return f"{self.rating:.1f} {self.rating_source.capitalize()}".strip()


def score(candidates: list[Candidate], cited_urls: list[str]) -> tuple[float, str]:
    """Score the sweep in ``[0, 1]`` and explain the score in one line.

    The four factors multiply rather than average, so one bad signal drags the
    result down whatever the others say. That is the right shape for a metric
    whose job is to catch mistakes, and it means a single-source sweep cannot
    exceed 0.33 however good its one source looks.
    """
    if not candidates:
        return 0.0, "no candidates"

    winner = min(candidates, key=lambda c: c.price)
    corroboration = min(len(candidates) / _TARGET_SOURCES, 1.0)
    outlier, outlier_reason = _outlier(candidates, winner)
    sourcing, sourcing_reason = _sourcing(winner, cited_urls)
    reputation, reputation_reason = _reputation(winner)

    value = round(corroboration * outlier * sourcing * reputation, 2)
    plural = "" if len(candidates) == 1 else "s"
    reason = (
        f"{len(candidates)} source{plural}, {outlier_reason}, "
        f"{sourcing_reason}, {reputation_reason}"
    )
    return value, reason


def _outlier(candidates: list[Candidate], winner: Candidate) -> tuple[float, str]:
    others = [c.price for c in candidates if c is not winner]
    if not others:
        # Nothing to compare against. Corroboration already penalises a sole
        # source; punishing it here too would put it beyond any evidence.
        return 1.0, "sole source"

    ratio = winner.price / median(others)
    gap = f"winner {1 - ratio:.0%} under median"
    if ratio >= _OUTLIER_FULL:
        return 1.0, gap
    if ratio <= _OUTLIER_ZERO:
        return 0.0, gap
    return (ratio - _OUTLIER_ZERO) / (_OUTLIER_FULL - _OUTLIER_ZERO), gap


def _sourcing(winner: Candidate, cited_urls: list[str]) -> tuple[float, str]:
    url = winner.url.strip()
    if not url.startswith(("http://", "https://")):
        return 0.3, "no source url"
    if url in {u.strip() for u in cited_urls if u}:
        return 1.0, "cited"
    return 0.6, "uncited url"


def _reputation(winner: Candidate) -> tuple[float, str]:
    """Score the winner's review rating.

    Ratings below ``FLOOR_RATING`` never arrive here — market.py drops those
    candidates outright — so the linear arm only ever spans 3.0 to 4.0.
    """
    if not winner.rated:
        return _UNKNOWN_RATING_SCORE, "unrated"
    if winner.rating >= _RATING_FULL:
        return 1.0, f"rated {winner.rating:.1f}"

    span = (winner.rating - FLOOR_RATING) / (_RATING_FULL - FLOOR_RATING)
    factor = _RATING_FLOOR_SCORE + span * (1.0 - _RATING_FLOOR_SCORE)
    return factor, f"rated {winner.rating:.1f}"
