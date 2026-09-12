"""The :class:`Candidate` value object and the confidence score built from it.

Grades a set of retailer candidates on how well it supports the claim "this is
the best UK price" — not on whether the winning number was read correctly. Pure:
no clients, no I/O, and no reliance on the model rating its own work, which is
the part of an LLM's output least worth trusting.
"""

from dataclasses import dataclass
from statistics import median

from price_monitor.models import supplier_domain

# A rating on fewer reviews than this is noise, and reads as unknown.
MIN_RATING_COUNT = 50

_TARGET_SOURCES = 3
# The outlier factor is full above _OUTLIER_FULL and zero at or below
# _OUTLIER_ZERO, sliding linearly between the two.
_OUTLIER_FULL = 0.8
_OUTLIER_ZERO = 0.4


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


def score(
    candidates: list[Candidate], winner: Candidate | None, cited_sites: list[str]
) -> tuple[float, str]:
    """Score the sweep in ``[0, 1]`` and explain the score in one line.

    ``winner`` is the candidate the sweep actually recorded as cheapest. It is
    passed in rather than re-derived here: `detections.select` breaks a price
    tie by rating, and a second `min` with a different tie-break would score
    the sourcing factor against a retailer other than the one on the sheet.

    The three factors multiply rather than average, so one bad signal drags the
    result down whatever the others say. That is the right shape for a metric
    whose job is to catch mistakes, and it means a single-source sweep cannot
    exceed 0.33 however good its one source looks.
    """
    if not candidates or winner is None:
        return 0.0, "no candidates"

    corroboration = min(len(candidates) / _TARGET_SOURCES, 1.0)
    outlier, outlier_reason = _outlier(candidates, winner)
    sourcing, sourcing_reason = _sourcing(winner, cited_sites)

    value = round(corroboration * outlier * sourcing, 2)
    plural = "" if len(candidates) == 1 else "s"
    reason = f"{len(candidates)} source{plural}, {outlier_reason}, {sourcing_reason}"
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


def _sourcing(winner: Candidate, cited_sites: list[str]) -> tuple[float, str]:
    """Did the model read the winner's price, or recall it?

    Compared by domain, not by exact URL. The model quotes a product page in
    its prose while the grounding metadata cites whatever it actually fetched,
    Compared against the site *names* Gemini cites, not its URIs: those are
    Vertex redirect links that never resemble the retailer's own address, so
    matching a product URL against them cannot succeed at all. Live sweeps
    scored every winner "uncited url" until that was understood.
    """
    url = winner.url.strip()
    if not url.startswith(("http://", "https://")):
        return 0.3, "no source url"
    domain = supplier_domain(url)
    if domain and any(_same_site(domain, site) for site in cited_sites):
        return 1.0, "cited"
    return 0.6, "uncited url"


def _same_site(domain: str, cited: str) -> bool:
    """True when a cited site name refers to the winner's domain.

    Gemini cites a site by name, which may be the bare domain or only its
    first label, so an equality test alone misses "Argos" against
    "argos.co.uk".
    """
    cited = cited.strip().lower()
    return bool(cited) and (domain == cited or domain.startswith(f"{cited}."))
