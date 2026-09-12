"""Selecting a sweep's three answers from its candidates.

Best-rated, cheapest, and the best-value compromise between them. Pure: no
clients, no I/O, no sheets. These rules are the part most likely to need tuning
once real sweeps are visible, and tuning them should cost a unit test rather
than a billed API call.
"""

from urllib.parse import urlparse

from price_monitor.models import Detection
from price_monitor.search.confidence import Candidate


def select(
    candidates: list[Candidate], premium_pct: float
) -> tuple[Candidate | None, Candidate | None, Candidate | None]:
    """Return ``(best_rated, cheapest, best_value)`` for one sweep.

    Returns the chosen candidates rather than finished detections so the caller
    can recover each winner's VAT note by identity — two candidates can share
    every field value, so matching a winner back by price and domain would pick
    the wrong one's note.

    All ranking happens here, on ``Candidate.rating``, which is a number.
    Ranking the display strings instead would sort "10.0 Google" below
    "4.4 Trustpilot".
    """
    if not candidates:
        return None, None, None

    rated = [c for c in candidates if c.rated]
    # Tie-breaks use the other dimension: best-rated prefers the cheaper, and
    # cheapest prefers the better-rated.
    best_rated = max(rated, key=lambda c: (c.rating, -c.price)) if rated else None
    cheapest = min(candidates, key=lambda c: (c.price, -(c.rating if c.rated else 0.0)))
    return best_rated, cheapest, _best_value(rated, premium_pct)


def _best_value(rated: list[Candidate], premium_pct: float) -> Candidate | None:
    """The best-rated retailer whose premium its rating gain pays for.

    The baseline is the cheapest *rated* candidate, not the cheapest overall:
    an unrated baseline has no rating to improve on, and treating it as zero
    would make every rated retailer look like a bargain worth a large premium.
    """
    if not rated:
        return None

    baseline = min(rated, key=lambda c: (c.price, -c.rating))
    qualifiers = [
        c
        for c in rated
        if c.rating > baseline.rating
        and c.price - baseline.price
        <= (c.rating - baseline.rating) * premium_pct * baseline.price
    ]
    if not qualifiers:
        return baseline
    return max(qualifiers, key=lambda c: (c.rating, -c.price))


def to_detection(candidate: Candidate | None) -> Detection | None:
    """Render a chosen candidate as the sheet's three cells."""
    if candidate is None:
        return None
    return Detection(
        price=candidate.price,
        site=supplier_domain(candidate.url),
        rating=candidate.rating_label,
    )


def supplier_domain(url: str) -> str:
    """The bare domain of ``url``, as a :class:`Detection`'s ``site`` value."""
    netloc = urlparse(url.strip()).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc
