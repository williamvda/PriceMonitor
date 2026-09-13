"""Batched retailer-ratings lookup.

Ratings move over months while prices move daily, so they are fetched on their
own slow cadence rather than during every sweep — where the model would spend
grounding passages reading Trustpilot instead of finding shops. Nothing raises
out of :meth:`RatingsLookup.fetch`: a ratings failure must never cost a sweep
its prices.
"""

import logging

from llmbridge import LLMClient
from llmbridge.exceptions import LLMError
from py_utils.logger import get_child_logger

from price_monitor.app_config import LLMConfig
from price_monitor.models import Rating, supplier_domain
from price_monitor.search.json_extract import extract_json
from price_monitor.search.searcher import FORMAT_PROVIDER, retrying

# One grounded call per update, whatever state the cache is in: a first run
# against fifty retailers spreads over five days rather than spending fifty
# billed searches at once.
RATING_BATCH = 10

# Ratings are out of 5. A value outside that range is not a trustworthy
# five-point score — the model may have reported one out of 10, or mistaken a
# review count for a score — so it is stored as no rating at all.
_MAX_RATING = 5.0

_SEARCH_TEMPLATE = """Find the customer review rating for each of these UK retailers.

Retailers:
{sites}

For each one report:
- its Trustpilot rating out of 5, and how many reviews that rating is based on
- if it has no Trustpilot page, its Google review rating instead, saying which
  of the two you used

If you cannot find a rating for a retailer, say unknown — do not estimate one.
An invented rating is worse than a missing one, because a missing one is
treated honestly as unrated."""

_FORMAT_TEMPLATE = """Convert the report below into one JSON object and nothing else.

Schema:
{{
  "ratings": [
    {{
      "site": "<retailer domain>",
      "rating": <number or null>,
      "reviews": <integer or null>,
      "source": "trustpilot" | "google" | ""
    }}
  ]
}}

Rules:
- One entry per retailer the report covers.
- rating and reviews are null when the report gives no rating for that
  retailer. Never invent one.
- source is "trustpilot" or "google" — whichever the report used — and "" when
  there is no rating.
- Output the JSON object only. No markdown fences, no commentary.

Report:
{report}"""


class RatingsLookup:
    """Fetches review ratings for several retailers in one grounded call."""

    def __init__(
        self,
        config: LLMConfig,
        logger: logging.Logger,
        search_client: LLMClient | None = None,
        format_client: LLMClient | None = None,
    ) -> None:
        self.logger = get_child_logger(logger, __class__.__name__)
        search_provider = config.provider if config.grounded else FORMAT_PROVIDER
        self._search_client = search_client or LLMClient(
            provider=search_provider,
            api_key=config.api_key,
            model=config.model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.timeout,
        )
        self._format_client = format_client or LLMClient(
            provider=FORMAT_PROVIDER,
            api_key=config.api_key,
            model=config.model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.timeout,
        )

    def fetch(self, sites: list[str]) -> dict[str, Rating]:
        """Look up ``sites``, answering for every one of them or for none.

        A site the report does not mention still comes back, with an empty
        rating, so its ``checked`` date is stamped and it is not looked up
        again tomorrow.
        """
        wanted = [site for site in sites if site.strip()]
        if not wanted:
            return {}

        try:
            searched = retrying(
                lambda: self._search_client.prompt(
                    _SEARCH_TEMPLATE.format(sites="\n".join(f"- {s}" for s in wanted))
                ),
                "ratings search",
                self.logger,
            )
            formatted = retrying(
                lambda: self._format_client.prompt(
                    _FORMAT_TEMPLATE.format(report=searched.text)
                ),
                "ratings format",
                self.logger,
            )
            payload = extract_json(formatted.text)
        except (LLMError, KeyError, IndexError, TypeError, ValueError) as exc:
            self.logger.warning(f"Ratings lookup failed: {exc}")
            return {}

        found = self._parse(payload)
        return {site: found.get(site, Rating()) for site in wanted}

    def _parse(self, payload: dict) -> dict[str, Rating]:
        entries = payload.get("ratings")
        if not isinstance(entries, list):
            return {}
        parsed: dict[str, Rating] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            site = supplier_domain(f"https://{str(entry.get('site') or '').strip()}")
            if not site:
                continue
            rating = _as_float(entry.get("rating"))
            if rating is not None and not (0.0 <= rating <= _MAX_RATING):
                rating = None
            parsed[site] = Rating(
                rating=rating,
                reviews=_as_int(entry.get("reviews")),
                source=str(entry.get("source") or "").strip().lower(),
            )
        return parsed


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
