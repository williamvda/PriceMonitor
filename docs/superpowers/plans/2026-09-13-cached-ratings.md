# Cached Retailer Ratings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move retailer ratings out of the price sweep into a cached `Ratings` tab with its own slow refresh, so the grounded call spends its effort on shops rather than on Trustpilot.

**Architecture:** A `Ratings` sheet tab stores one row per retailer. The sweep registers every retailer it saw; a batched lookup, folded into the existing `update()`, rates whatever is unrated or stale. `Candidate` ratings come from that cache instead of from the model's reply, and the rating request leaves the sweep prompt.

**Tech Stack:** Python 3.11, pandas, pytest, llmbridge (Gemini), google-drive-api. Black + isort + flake8.

**Spec:** `docs/superpowers/specs/2026-09-13-cached-ratings-design.md`

## Global Constraints

- Branch: `feat/control-tab-and-sweep-reliability`, already checked out. Do not merge or push.
- PEP 604 unions (`X | None`), never `Optional`. No `from __future__ import annotations`.
- Annotate every function signature and dataclass field. `@dataclass(frozen=True)` for value objects.
- Module docstring on every `.py` file, accurate to its public surface.
- Comments only for non-obvious invariants; narrating what code does is a defect.
- Strict KISS/YAGNI. No new dependencies. No ABCs, no dependency injection beyond the existing optional client parameters.
- `sheets/` and `msgserver_client/` must never import from `search/`. Shared value objects live in `models.py`.
- stdlib → third-party → local imports, no wildcards.
- Before every commit: `black src tests && isort src tests && flake8 src tests`, then `pytest tests/unit -q` must fully pass.
- **Never run `pytest tests/integration` against live services** — billed API calls against the user's credentials. `--collect-only` is fine.
- No Co-Authored-By or attribution line in any commit message.
- TDD: failing test first, run it, then implement.

### Two invariants that must not be broken

**Trailing cells.** The Sheets API omits trailing empty cells. When every row in a read comes back short, pandas raises *inside* `GoogleSheetInterface.read()` before our code sees it, and the tab becomes unreadable. On the `Ratings` tab, `checked` must stay last.

**`write()` clears before it writes.** Any write to a tab we could not read risks destroying data we cannot see. Every write in this plan is conditional on a successful read. This is not defensive style — it is the bug that was caught in the `ControlTab` design and it must not return.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `src/price_monitor/app_config.py` | MODIFY — `ratings_sheet`, `rating_rate_d` |
| `src/price_monitor/models.py` | MODIFY — the `Rating` value object |
| `src/price_monitor/sheets/ratings_tab.py` | CREATE — read, register, write the Ratings tab |
| `src/price_monitor/search/ratings.py` | CREATE — `RatingsLookup`, its prompts, `RATING_BATCH` |
| `src/price_monitor/search/market.py` | MODIFY — ratings passed in; stop reading them from the payload |
| `src/price_monitor/search/prompts.py` | MODIFY — the rating request leaves both sweep templates |
| `src/price_monitor/price_monitor.py` | MODIFY — refresh before sweeps, register after |
| `config/config.example.json`, `README.md` | MODIFY — new settings and the new tab |

---

## Task 1: The Rating value object and config

**Files:**
- Modify: `src/price_monitor/models.py`, `src/price_monitor/app_config.py`, `config/config.example.json`
- Test: `tests/unit/test_models.py`, `tests/unit/test_app_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Rating(rating: float | None = None, reviews: int | None = None, source: str = "")`, frozen; `PriceCtrl.ratings_sheet: str = "Ratings"`; `PriceCtrl.rating_rate_d: float = 30.0`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_models.py`:

```python
def test_a_rating_carries_its_score_count_and_source():
    from price_monitor.models import Rating

    rating = Rating(rating=4.1, reviews=13305, source="trustpilot")
    assert rating.rating == 4.1
    assert rating.reviews == 13305
    assert rating.source == "trustpilot"


def test_a_rating_defaults_to_nothing_known():
    """A site that was looked up and had no rating is a real state, and it is
    distinct from one that has never been looked up."""
    from price_monitor.models import Rating

    rating = Rating()
    assert rating.rating is None
    assert rating.reviews is None
    assert rating.source == ""
```

Append to `tests/unit/test_app_config.py`:

```python
def test_the_ratings_cache_defaults():
    ctrl = PriceCtrl()
    assert ctrl.ratings_sheet == "Ratings"
    assert ctrl.rating_rate_d == 30.0
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_models.py tests/unit/test_app_config.py -q`
Expected: FAIL — `ImportError: cannot import name 'Rating'` and `AttributeError: 'PriceCtrl' object has no attribute 'ratings_sheet'`.

- [ ] **Step 3: Implement**

In `src/price_monitor/models.py`, above `PriceReading`:

```python
@dataclass(frozen=True)
class Rating:
    """One retailer's review score, as cached rather than as freshly asked.

    An all-default ``Rating`` means the site was looked up and nothing was
    found — a different and much cheaper state than never having asked, which
    is represented by the site being absent from the cache entirely.
    """

    rating: float | None = None
    reviews: int | None = None
    source: str = ""
```

In `PriceCtrl`, after `control_sheet`:

```python
    ratings_sheet: str = "Ratings"
```

and after `report_keep_days`:

```python
    # Ratings move over months while prices move daily, so the cache is
    # refreshed on its own slow cadence rather than on every sweep.
    rating_rate_d: float = 30.0
```

In `config/config.example.json`, add `"ratings_sheet": "Ratings"` beside `"control_sheet"` and `"rating_rate_d": 30` beside `"report_keep_days"`.

- [ ] **Step 4: Run the full suite**

Run: `pytest tests/unit -q`
Expected: PASS — purely additive.

- [ ] **Step 5: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/models.py src/price_monitor/app_config.py config/config.example.json tests/unit
git commit -m "feat: add the Rating value object and ratings cache settings"
```

---

## Task 2: The Ratings tab

**Files:**
- Create: `src/price_monitor/sheets/ratings_tab.py`
- Test: `tests/unit/test_ratings_tab.py`

**Interfaces:**
- Consumes: `Rating` (Task 1).
- Produces:
  - `COLUMNS = ["site", "rating", "reviews", "source", "checked"]`
  - `RatingsTab(gsheet, sheet_name, logger, notify=None)`
  - `.read() -> dict[str, Rating] | None` — `None` when the tab could not be read.
  - `.stale(within_days: float) -> list[str]` — sites needing a lookup, registered-but-never-checked first.
  - `.register(sites: set[str]) -> None` — append placeholder rows for unseen sites.
  - `.store(results: dict[str, Rating]) -> None` — write results, stamping `checked` for every site passed.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_ratings_tab.py`. Model the fake on `tests/unit/test_control_tab.py`, which is the closest sibling:

```python
"""Tests for the cached Ratings tab."""

import logging
from datetime import datetime, timedelta

import pandas as pd
import pytest

from price_monitor.models import Rating
from price_monitor.sheets.ratings_tab import COLUMNS, RatingsTab

TODAY = datetime.now().strftime("%Y-%m-%d")


class FakeSheet:
    def __init__(self, frame: pd.DataFrame | None = None, error: Exception | None = None):
        self.frame = frame if frame is not None else pd.DataFrame()
        self.error = error
        self.writes: list[str] = []

    def read(self, sheet_name: str) -> pd.DataFrame:
        if self.error is not None:
            raise self.error
        return self.frame.copy()

    def write(self, sheet_name: str, df: pd.DataFrame) -> None:
        self.writes.append(sheet_name)
        self.frame = df.copy()
        self.error = None


@pytest.fixture
def logger():
    return logging.getLogger("test")


def _rows(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLUMNS)


def _row(site: str, rating: str = "4.1", checked: str = TODAY) -> dict:
    return {
        "site": site,
        "rating": rating,
        "reviews": "900",
        "source": "trustpilot",
        "checked": checked,
    }


def test_columns_end_with_checked_so_trailing_cells_are_never_trimmed():
    assert COLUMNS[-1] == "checked"


def test_ratings_are_read_by_site(logger):
    tab = RatingsTab(FakeSheet(_rows([_row("argos.co.uk")])), "Ratings", logger)
    ratings = tab.read()
    assert ratings["argos.co.uk"].rating == 4.1
    assert ratings["argos.co.uk"].reviews == 900
    assert ratings["argos.co.uk"].source == "trustpilot"


def test_a_checked_site_with_no_rating_reads_as_a_known_blank(logger):
    """'We asked and there was nothing' must be distinguishable from
    'we have not asked', or it would be looked up forever."""
    frame = _rows([{"site": "tiny.co.uk", "rating": "", "reviews": "", "source": "", "checked": TODAY}])
    ratings = RatingsTab(FakeSheet(frame), "Ratings", logger).read()
    assert "tiny.co.uk" in ratings
    assert ratings["tiny.co.uk"].rating is None


def test_an_empty_tab_reads_as_no_ratings(logger):
    assert RatingsTab(FakeSheet(), "Ratings", logger).read() == {}


def test_an_unreadable_tab_returns_none_and_is_never_written(logger):
    """write() clears before it writes, so a tab we cannot read must not be
    touched — it may still hold every rating we have."""
    sheet = FakeSheet(error=ValueError("2 columns passed, passed data had 1 columns"))
    tab = RatingsTab(sheet, "Ratings", logger)
    assert tab.read() is None
    tab.register({"argos.co.uk"})
    tab.store({"argos.co.uk": Rating(4.1, 900, "trustpilot")})
    assert sheet.writes == []


def test_a_missing_tab_is_created_empty(logger):
    sheet = FakeSheet(error=RuntimeError("Unable to parse range: Ratings"))
    assert RatingsTab(sheet, "Ratings", logger).read() == {}
    assert sheet.writes == ["Ratings"]
    assert list(sheet.frame.columns) == COLUMNS


def test_stale_includes_sites_never_checked(logger):
    frame = _rows([{"site": "new.co.uk", "rating": "", "reviews": "", "source": "", "checked": ""}])
    assert RatingsTab(FakeSheet(frame), "Ratings", logger).stale(30) == ["new.co.uk"]


def test_stale_includes_sites_checked_too_long_ago(logger):
    old = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    frame = _rows([_row("argos.co.uk", checked=old)])
    assert RatingsTab(FakeSheet(frame), "Ratings", logger).stale(30) == ["argos.co.uk"]


def test_stale_excludes_a_recently_checked_site(logger):
    frame = _rows([_row("argos.co.uk", checked=TODAY)])
    assert RatingsTab(FakeSheet(frame), "Ratings", logger).stale(30) == []


def test_an_unparseable_date_counts_as_never_checked(logger):
    """A hand-edited cell must make a site stale, never permanently fresh."""
    frame = _rows([_row("argos.co.uk", checked="last tuesday")])
    assert RatingsTab(FakeSheet(frame), "Ratings", logger).stale(30) == ["argos.co.uk"]


def test_never_checked_sites_come_before_merely_stale_ones(logger):
    """A retailer we have never rated is worth more than refreshing one we
    already have a usable number for."""
    old = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    frame = _rows(
        [
            _row("old.co.uk", checked=old),
            {"site": "new.co.uk", "rating": "", "reviews": "", "source": "", "checked": ""},
        ]
    )
    assert RatingsTab(FakeSheet(frame), "Ratings", logger).stale(30) == [
        "new.co.uk",
        "old.co.uk",
    ]


def test_register_appends_only_unseen_sites(logger):
    sheet = FakeSheet(_rows([_row("argos.co.uk")]))
    RatingsTab(sheet, "Ratings", logger).register({"argos.co.uk", "new.co.uk"})
    assert set(sheet.frame["site"]) == {"argos.co.uk", "new.co.uk"}
    assert sheet.frame[sheet.frame["site"] == "new.co.uk"].iloc[0]["checked"] == ""


def test_register_writes_nothing_when_every_site_is_known(logger):
    sheet = FakeSheet(_rows([_row("argos.co.uk")]))
    RatingsTab(sheet, "Ratings", logger).register({"argos.co.uk"})
    assert sheet.writes == []


def test_store_stamps_checked_even_for_a_site_with_no_rating(logger):
    """Otherwise a retailer with no Trustpilot presence is looked up forever."""
    sheet = FakeSheet(_rows([{"site": "tiny.co.uk", "rating": "", "reviews": "", "source": "", "checked": ""}]))
    RatingsTab(sheet, "Ratings", logger).store({"tiny.co.uk": Rating()})
    row = sheet.frame.iloc[0]
    assert row["checked"] == TODAY
    assert row["rating"] == ""


def test_store_updates_an_existing_row_rather_than_duplicating_it(logger):
    sheet = FakeSheet(_rows([_row("argos.co.uk", rating="3.0")]))
    RatingsTab(sheet, "Ratings", logger).store(
        {"argos.co.uk": Rating(4.4, 1200, "trustpilot")}
    )
    assert len(sheet.frame) == 1
    assert sheet.frame.iloc[0]["rating"] == "4.4"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_ratings_tab.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'price_monitor.sheets.ratings_tab'`.

- [ ] **Step 3: Implement**

Create `src/price_monitor/sheets/ratings_tab.py`:

```python
"""Cached retailer ratings.

One row per retailer, refreshed on its own slow cadence rather than on every
sweep. A row with a ``checked`` date and no rating records that the retailer
was looked up and had none — a different state from never having asked, which
is a site being absent from the tab entirely.
"""

import logging
from datetime import datetime, timedelta
from typing import Callable

import pandas as pd
from py_utils.logger import get_child_logger

from price_monitor.models import Rating
from price_monitor.sheets.protocol import SheetInterface

DATE_FORMAT = "%Y-%m-%d"

# `checked` MUST stay last. The Sheets API omits trailing empty cells, and when
# every row in a read comes back short pandas raises inside
# GoogleSheetInterface.read() before this module sees it. Every other column is
# blank for a site that has been registered but not yet rated.
COLUMNS = ["site", "rating", "reviews", "source", "checked"]


class RatingsTab:
    """Reads and maintains the cached ratings, never writing over what it cannot read."""

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

    def read(self) -> dict[str, Rating] | None:
        """Cached ratings by site, or ``None`` when the tab could not be read."""
        frame = self._frame()
        if frame is None:
            return None
        return {
            site: Rating(
                rating=_as_float(row["rating"]),
                reviews=_as_int(row["reviews"]),
                source=str(row["source"] or "").strip(),
            )
            for row in frame.to_dict("records")
            if (site := str(row["site"] or "").strip())
        }

    def stale(self, within_days: float) -> list[str]:
        """Sites needing a lookup, never-checked ones first.

        A site we have never rated is worth more than refreshing one we already
        have a usable number for, so the batch cap spends itself on the former.
        """
        frame = self._frame()
        if frame is None:
            return []
        cutoff = datetime.now() - timedelta(days=within_days)
        never, old = [], []
        for row in frame.to_dict("records"):
            site = str(row["site"] or "").strip()
            if not site:
                continue
            checked = _as_date(row["checked"])
            if checked is None:
                never.append(site)
            elif checked < cutoff:
                old.append(site)
        return never + old

    def register(self, sites: set[str]) -> None:
        """Add placeholder rows for sites the cache has not seen.

        Only the three winning detections reach the history, so without this a
        retailer that competed and lost would never be known about, and could
        never become best-rated.
        """
        frame = self._frame()
        if frame is None:
            return
        known = {str(site).strip() for site in frame["site"]}
        unseen = sorted({s.strip() for s in sites if s.strip()} - known)
        if not unseen:
            return
        additions = pd.DataFrame(
            [{**{column: "" for column in COLUMNS}, "site": site} for site in unseen],
            columns=COLUMNS,
        )
        self._write(pd.concat([frame, additions], ignore_index=True))
        self.logger.info(f"Registered {len(unseen)} new retailer(s) for rating")

    def store(self, results: dict[str, Rating]) -> None:
        """Record a lookup's results, stamping ``checked`` for every site asked.

        A site that came back without a rating is stamped too: otherwise a
        retailer with no Trustpilot or Google presence would be looked up on
        every refresh, forever.
        """
        frame = self._frame()
        if frame is None or not results:
            return
        today = datetime.now().strftime(DATE_FORMAT)
        rows = {str(row["site"]).strip(): dict(row) for row in frame.to_dict("records")}
        for site, rating in results.items():
            rows[site] = {
                "site": site,
                "rating": "" if rating.rating is None else f"{rating.rating:g}",
                "reviews": "" if rating.reviews is None else str(rating.reviews),
                "source": rating.source,
                "checked": today,
            }
        self._write(pd.DataFrame(list(rows.values()), columns=COLUMNS))

    def _frame(self) -> pd.DataFrame | None:
        """The tab's contents, or ``None`` when it could not be read.

        A tab that does not exist fails in the Sheets API, which cannot parse
        the range — that is a first run, and creating it is safe. A tab whose
        every row is short of its header fails in pandas, locally, which means
        it exists and still holds data. ``write()`` clears before it writes, so
        that one must never be written to.
        """
        try:
            frame = self._gsheet.read(sheet_name=self._sheet_name)
        except ValueError as exc:
            message = (
                f"The '{self._sheet_name}' tab could not be read ({exc}), so no "
                f"cached ratings are being used and none will be written."
            )
            self.logger.warning(message)
            self._notify(f"⚠️ PriceMonitor — {message}")
            return None
        except Exception as exc:
            self.logger.info(f"No '{self._sheet_name}' tab ({exc}); creating it.")
            self._write(pd.DataFrame(columns=COLUMNS))
            return pd.DataFrame(columns=COLUMNS)

        if frame.empty:
            return pd.DataFrame(columns=COLUMNS)
        for column in COLUMNS:
            if column not in frame.columns:
                frame[column] = ""
        return frame[COLUMNS].fillna("")

    def _write(self, frame: pd.DataFrame) -> None:
        try:
            self._gsheet.write(sheet_name=self._sheet_name, df=frame)
        except Exception as exc:
            self.logger.warning(f"Could not write '{self._sheet_name}': {exc}")


def _as_float(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _as_date(value: object) -> datetime | None:
    try:
        return datetime.strptime(str(value).strip(), DATE_FORMAT)
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/test_ratings_tab.py -v`
Expected: PASS, all cases.

- [ ] **Step 5: Run the full suite**

Run: `pytest tests/unit -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/sheets/ratings_tab.py tests/unit/test_ratings_tab.py
git commit -m "feat: add the cached Ratings tab"
```

---

## Task 3: The batched ratings lookup

**Files:**
- Create: `src/price_monitor/search/ratings.py`
- Test: `tests/unit/test_ratings.py`

**Interfaces:**
- Consumes: `Rating` (Task 1); existing `retrying`, `FORMAT_PROVIDER`, `extract_json`.
- Produces:
  - `RATING_BATCH = 10`
  - `RatingsLookup(config: LLMConfig, logger: logging.Logger, search_client=None, format_client=None)`
  - `.fetch(sites: list[str]) -> dict[str, Rating]` — one entry per site *requested*, never raising.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_ratings.py`. Model `FakeClient` and `_response` on `tests/unit/test_market.py`:

```python
"""Unit tests for the batched retailer-ratings lookup."""

import json
import logging

import pytest
from llmbridge.exceptions import LLMConnectionError
from llmbridge.models import PromptResponse

from price_monitor.app_config import LLMConfig
from price_monitor.search.ratings import RATING_BATCH, RatingsLookup


def _response(text: str) -> PromptResponse:
    return PromptResponse(
        text=text,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        model="gemini-3.7-flash",
        provider="gemini",
        latency_ms=1.0,
        raw_response={},
    )


class FakeClient:
    def __init__(self, *results):
        self.results = list(results)
        self.prompts: list[str] = []

    def prompt(self, prompt_text: str, **kwargs) -> PromptResponse:
        self.prompts.append(prompt_text)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def logger():
    return logging.getLogger("test")


def _lookup(logger, payload: str, report: str = "a report") -> RatingsLookup:
    return RatingsLookup(
        LLMConfig(api_key="k"),
        logger,
        search_client=FakeClient(_response(report)),
        format_client=FakeClient(_response(payload)),
    )


def _payload(entries: list[dict]) -> str:
    return json.dumps({"ratings": entries})


def test_a_rating_is_read_back_for_the_site_requested(logger):
    lookup = _lookup(
        logger,
        _payload([{"site": "argos.co.uk", "rating": 4.1, "reviews": 13305, "source": "trustpilot"}]),
    )
    ratings = lookup.fetch(["argos.co.uk"])
    assert ratings["argos.co.uk"].rating == 4.1
    assert ratings["argos.co.uk"].reviews == 13305


def test_a_site_that_was_asked_about_but_not_returned_is_still_answered(logger):
    """Every requested site comes back, so its `checked` gets stamped and it
    is not looked up again tomorrow."""
    lookup = _lookup(logger, _payload([]))
    ratings = lookup.fetch(["tiny.co.uk"])
    assert set(ratings) == {"tiny.co.uk"}
    assert ratings["tiny.co.uk"].rating is None


def test_a_site_returned_but_never_asked_about_is_ignored(logger):
    lookup = _lookup(
        logger,
        _payload([{"site": "elsewhere.co.uk", "rating": 4.0, "reviews": 100, "source": "google"}]),
    )
    assert set(lookup.fetch(["argos.co.uk"])) == {"argos.co.uk"}


def test_a_rating_outside_the_five_point_scale_is_stored_as_none(logger):
    """A rating out of 10, or a review count mistaken for a score, is not a
    trustworthy five-point value."""
    lookup = _lookup(
        logger,
        _payload([{"site": "argos.co.uk", "rating": 45.0, "reviews": 100, "source": "google"}]),
    )
    assert lookup.fetch(["argos.co.uk"])["argos.co.uk"].rating is None


def test_a_boolean_rating_is_not_read_as_one(logger):
    lookup = _lookup(
        logger,
        _payload([{"site": "argos.co.uk", "rating": True, "reviews": 100, "source": "google"}]),
    )
    assert lookup.fetch(["argos.co.uk"])["argos.co.uk"].rating is None


def test_matching_ignores_www_and_case(logger):
    lookup = _lookup(
        logger,
        _payload([{"site": "WWW.Argos.co.uk", "rating": 4.1, "reviews": 900, "source": "trustpilot"}]),
    )
    assert lookup.fetch(["argos.co.uk"])["argos.co.uk"].rating == 4.1


def test_an_unparseable_reply_answers_every_site_with_nothing(logger):
    lookup = _lookup(logger, "not json at all")
    ratings = lookup.fetch(["argos.co.uk"])
    assert ratings == {}


def test_a_raising_client_never_escapes(logger):
    """A ratings failure must not cost a sweep its prices."""
    lookup = RatingsLookup(
        LLMConfig(api_key="k"),
        logger,
        search_client=FakeClient(
            LLMConnectionError("down"), LLMConnectionError("down"), LLMConnectionError("down")
        ),
        format_client=FakeClient(_response("{}")),
    )
    assert lookup.fetch(["argos.co.uk"]) == {}


def test_no_sites_makes_no_calls(logger):
    search = FakeClient()
    lookup = RatingsLookup(
        LLMConfig(api_key="k"), logger, search_client=search, format_client=FakeClient()
    )
    assert lookup.fetch([]) == {}
    assert search.prompts == []


def test_the_prompt_names_every_site_and_forbids_estimating(logger):
    lookup = _lookup(logger, _payload([]))
    lookup.fetch(["argos.co.uk", "currys.co.uk"])
    prompt = lookup._search_client.prompts[0]
    assert "argos.co.uk" in prompt
    assert "currys.co.uk" in prompt
    assert "do not estimate one" in prompt


def test_the_batch_size_is_ten():
    """One grounded call per update, whatever state the cache is in."""
    assert RATING_BATCH == 10
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_ratings.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'price_monitor.search.ratings'`.

- [ ] **Step 3: Implement**

Create `src/price_monitor/search/ratings.py`:

```python
"""Batched retailer-ratings lookup.

Ratings move over months while prices move daily, so they are fetched on their
own slow cadence rather than during every sweep — where the model would spend
grounding passages reading Trustpilot instead of finding shops. Nothing raises
out of :meth:`fetch`: a ratings failure must never cost a sweep its prices.
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
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/test_ratings.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `pytest tests/unit -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/search/ratings.py tests/unit/test_ratings.py
git commit -m "feat: look up retailer ratings in batches"
```

---

## Task 4: The sweep takes its ratings from the cache

**Files:**
- Modify: `src/price_monitor/search/market.py`, `src/price_monitor/search/prompts.py`
- Test: `tests/unit/test_market.py`, `tests/unit/test_prompts.py`

**Interfaces:**
- Consumes: `Rating` (Task 1).
- Produces: `MarketSearcher.price_market(item, last_price, timestamp, blocked=None, ratings=None)`; `MarketSearcher.seen_sites` — the domains of the candidates the last sweep kept.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_market.py`:

```python
def test_a_candidates_rating_comes_from_the_cache_not_the_reply(logger):
    """The model no longer reports ratings; a stale-but-stable cached value is
    better than one that changes source between runs."""
    searcher = _searcher(logger, [_candidate(price=100.0, url="https://argos.co.uk/w")])
    reading = searcher.price_market(
        ITEM, None, NOW, ratings={"argos.co.uk": Rating(4.4, 900, "trustpilot")}
    )
    assert reading.cheapest.rating == "4.4 Trustpilot"


def test_an_uncached_retailer_is_unrated_but_still_competes(logger):
    searcher = _searcher(logger, [_candidate(price=100.0, url="https://new.co.uk/w")])
    reading = searcher.price_market(ITEM, None, NOW, ratings={})
    assert reading.cheapest.price == 100.0
    assert reading.cheapest.rating == ""
    assert reading.best_rated is None


def test_a_cached_rating_on_too_few_reviews_is_still_untrusted(logger):
    """MIN_RATING_COUNT governs whether a rating is trusted, which is a
    separate question from whether it is known."""
    searcher = _searcher(logger, [_candidate(price=100.0, url="https://argos.co.uk/w")])
    reading = searcher.price_market(
        ITEM, None, NOW, ratings={"argos.co.uk": Rating(4.9, 12, "trustpilot")}
    )
    assert reading.cheapest.rating == ""


def test_the_sweep_reports_the_sites_it_kept(logger):
    """Only winners reach the history, so the losers must be registered from
    here or they could never be rated."""
    searcher = _searcher(
        logger,
        [
            _candidate(price=100.0, url="https://argos.co.uk/w"),
            _candidate(price=120.0, url="https://currys.co.uk/w"),
        ],
    )
    searcher.price_market(ITEM, None, NOW)
    assert searcher.seen_sites == {"argos.co.uk", "currys.co.uk"}
```

Append to `tests/unit/test_prompts.py`:

```python
def test_the_sweep_no_longer_asks_for_ratings():
    """Gemini spent grounding passages reading Trustpilot because we asked it
    to. Those passages are wanted for finding shops."""
    prompt = market_prompt(Item(name="Widget"))
    assert "Trustpilot" not in prompt
    assert "rating" not in prompt.lower()


def test_the_format_schema_no_longer_carries_rating_fields():
    schema = market_format_prompt("report", [])
    assert "rating_count" not in schema
    assert "rating_source" not in schema
```

Add `from price_monitor.models import Rating` to `test_market.py`'s imports.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_market.py tests/unit/test_prompts.py -q`
Expected: FAIL — `price_market()` takes no `ratings` argument, `seen_sites` does not exist, and the prompts still mention Trustpilot.

- [ ] **Step 3: Strip ratings from the prompts**

In `src/price_monitor/search/prompts.py`, delete from `_MARKET_TEMPLATE` the whole paragraph beginning "For each retailer also report its Trustpilot rating" and ending "do not estimate one." Delete from `_MARKET_FORMAT_TEMPLATE` the `"rating"`, `"rating_count"` and `"rating_source"` schema lines and the two rules describing them.

- [ ] **Step 4: Take ratings from the cache**

In `src/price_monitor/search/market.py`:

Add `from price_monitor.models import Rating` to the models import, and `self.seen_sites: set[str] = set()` to `__init__`.

Add `ratings: dict[str, Rating] | None = None` to `price_market`'s signature, after `blocked`, and thread it through to `_rank` and then `_candidates` exactly as `blocked` already is.

In `_candidates`, replace the block that reads `rating`, `rating_count` and `rating_source` from `raw` with a cache lookup, and record the site:

```python
            cached = (ratings or {}).get(site, Rating())
            self.seen_sites.add(site)
            candidate = Candidate(
                supplier=str(raw.get("supplier") or "").strip(),
                price=price,
                url=str(raw.get("url") or "").strip(),
                in_stock=True,
                rating=cached.rating,
                rating_count=cached.reviews,
                rating_source=cached.source,
            )
```

Reset `self.seen_sites` at the top of `_candidates` so it reports one sweep, not every sweep since start-up. Delete `MAX_RATING` and its comment — that validation now lives in `search/ratings.py`, where the number enters the system.

- [ ] **Step 5: Run the tests**

Run: `pytest tests/unit -q`

Three existing `test_market.py` tests are about ratings arriving in the model's
payload, which no longer happens. Their subject has moved, so **delete** them
rather than trying to port them — each already has a replacement:

| Delete from `test_market.py` | Replaced by |
| --- | --- |
| `test_an_out_of_range_rating_is_treated_as_unrated` | `test_ratings.py::test_a_rating_outside_the_five_point_scale_is_stored_as_none` (Task 3) |
| `test_a_boolean_rating_is_treated_as_unrated_not_as_one` | `test_ratings.py::test_a_boolean_rating_is_not_read_as_one` (Task 3) |
| `test_a_bad_rating_on_too_few_reviews_reads_as_unrated` | `test_market.py::test_a_cached_rating_on_too_few_reviews_is_still_untrusted` (Task 4, Step 1) |

Confirm each replacement exists and passes before deleting its predecessor —
that is what makes this a move rather than a loss. Any other test that sets
`rating`, `rating_count` or `rating_source` in a candidate payload should have
those fields removed; they are now ignored.

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/search tests/unit
git commit -m "feat: take candidate ratings from the cache, not the sweep"
```

---

## Task 5: Wire the refresh into update()

**Files:**
- Modify: `src/price_monitor/price_monitor.py`
- Test: `tests/unit/test_price_monitor.py`

**Interfaces:**
- Consumes: `RatingsTab` (Task 2), `RatingsLookup` (Task 3), `MarketSearcher.seen_sites` (Task 4).
- Produces: nothing later tasks rely on.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_price_monitor.py`:

```python
def test_stale_ratings_are_refreshed_before_the_sweeps(logger):
    """The sweep needs the cache, so filling it afterwards would waste a day."""
    order: list[str] = []
    sheet = _sheet_with_history([_history_row("Widget", datetime.now() - _STALE)], ["Widget"])
    searcher = FakeMarketSearcher()
    monitor = _monitor(sheet, searcher, logger)
    monitor.ratings_tab = _FakeRatingsTab(stale=["argos.co.uk"], on_call=order.append)
    monitor.ratings_lookup = _FakeLookup(on_call=order.append)
    monitor.update()
    assert order.index("fetch") < order.index("register")


def test_sites_the_sweep_saw_are_registered_afterwards(logger):
    sheet = _sheet_with_history([_history_row("Widget", datetime.now() - _STALE)], ["Widget"])
    searcher = FakeMarketSearcher()
    searcher.seen_sites = {"argos.co.uk", "currys.co.uk"}
    monitor = _monitor(sheet, searcher, logger)
    tab = _FakeRatingsTab()
    monitor.ratings_tab = tab
    monitor.update()
    assert tab.registered == {"argos.co.uk", "currys.co.uk"}


def test_nothing_stale_makes_no_lookup(logger):
    sheet = _sheet_with_history([_history_row("Widget", datetime.now() - _STALE)], ["Widget"])
    monitor = _monitor(sheet, FakeMarketSearcher(), logger)
    monitor.ratings_tab = _FakeRatingsTab(stale=[])
    lookup = _FakeLookup()
    monitor.ratings_lookup = lookup
    monitor.update()
    assert lookup.fetched == []


def test_only_one_batch_is_looked_up_per_run(logger):
    """A first run against fifty retailers must not spend fifty searches."""
    sheet = _sheet_with_history([_history_row("Widget", datetime.now() - _STALE)], ["Widget"])
    monitor = _monitor(sheet, FakeMarketSearcher(), logger)
    monitor.ratings_tab = _FakeRatingsTab(stale=[f"shop{n}.co.uk" for n in range(50)])
    lookup = _FakeLookup()
    monitor.ratings_lookup = lookup
    monitor.update()
    assert len(lookup.fetched[0]) == 10


def test_a_ratings_failure_does_not_stop_the_sweep(logger):
    """Ratings are a nicety; prices are the job."""
    sheet = _sheet_with_history([_history_row("Widget", datetime.now() - _STALE)], ["Widget"])
    searcher = FakeMarketSearcher()
    monitor = _monitor(sheet, searcher, logger)
    monitor.ratings_tab = _FakeRatingsTab(stale=["argos.co.uk"], raises=RuntimeError("sheet down"))
    monitor.update()
    assert searcher.calls == ["Widget"]
```

Add the two fakes near the other test doubles in that file:

```python
class _FakeRatingsTab:
    def __init__(self, stale=None, raises=None, on_call=None):
        self._stale = stale if stale is not None else []
        self._raises = raises
        self._on_call = on_call or (lambda _name: None)
        self.registered: set[str] = set()
        self.stored: dict = {}

    def read(self):
        if self._raises is not None:
            raise self._raises
        return {}

    def stale(self, within_days):
        if self._raises is not None:
            raise self._raises
        return self._stale

    def register(self, sites):
        self._on_call("register")
        self.registered = set(sites)

    def store(self, results):
        self.stored = results


class _FakeLookup:
    def __init__(self, on_call=None):
        self._on_call = on_call or (lambda _name: None)
        self.fetched: list[list[str]] = []

    def fetch(self, sites):
        self._on_call("fetch")
        self.fetched.append(list(sites))
        return {}
```

Give `FakeMarketSearcher` a `self.seen_sites: set[str] = set()` attribute in its `__init__`.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_price_monitor.py -q`
Expected: FAIL — `PriceMonitor` has no `ratings_tab` or `ratings_lookup`.

- [ ] **Step 3: Implement**

In `src/price_monitor/price_monitor.py`:

Import `RatingsTab` from `price_monitor.sheets.ratings_tab`, and both
`RatingsLookup` and `RATING_BATCH` from `price_monitor.search.ratings`. In
`_init_parts`, after the control tab:

```python
        self.ratings_tab = RatingsTab(
            gsheet, self.ctrl.ratings_sheet, self.logger, notify=self.notifier.notify
        )
```

In `__init__`, build the lookup alongside the searcher:

```python
        self.ratings_lookup = RatingsLookup(config.llm_config, logger)
```

and set `monitor.ratings_lookup = None` in `for_test` before `_init_parts`, so tests that do not care never make a call.

In `update()`, before the sweeps:

```python
        self._refresh_ratings()
```

and add:

```python
    def _refresh_ratings(self) -> None:
        """Rate up to one batch of unrated or stale retailers.

        Bounded to a single grounded call per run whatever state the cache is
        in, so a first run against many retailers spreads over days rather
        than spending a search on each at once. A failure here logs and
        returns: ratings are a nicety, prices are the job.
        """
        if self.ratings_lookup is None:
            return
        try:
            stale = self.ratings_tab.stale(self.ctrl.rating_rate_d)[:RATING_BATCH]
            if not stale:
                return
            self.logger.info(f"Rating {len(stale)} retailer(s)")
            self.ratings_tab.store(self.ratings_lookup.fetch(stale))
        except Exception as exc:
            self.logger.warning(f"Ratings refresh failed: {exc}")
```

In `_price_all`, read the cache once and pass it to each sweep alongside `blocked`:

```python
        ratings = self.ratings_tab.read() or {}
```

and after the loop, register what the sweep saw:

```python
        self._register_seen()
```

with:

```python
    def _register_seen(self) -> None:
        """Add retailers this run saw to the ratings cache.

        Only the three winning detections reach the history, so a retailer
        that competed and lost would otherwise never be known about — and
        could never become best-rated.
        """
        seen = getattr(self._searcher, "seen_sites", set())
        if not seen:
            return
        try:
            self.ratings_tab.register(seen)
        except Exception as exc:
            self.logger.warning(f"Could not register retailers: {exc}")
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/price_monitor.py tests/unit/test_price_monitor.py
git commit -m "feat: refresh the ratings cache from update()"
```

---

## Task 6: Documentation

**Files:**
- Modify: `README.md`

**Interfaces:** none.

- [ ] **Step 1: Document the Ratings tab**

Read `README.md` in full first and match its register — it explains *why*, not just *what*.

Add a section beside the Control tab's covering: the five columns and that `checked` must stay last; that the tab is tool-written but hand-editable, which matters because the model has reported the same retailer as `4.0 Trustpilot`, `4.2 Google` and `4.2 Trustpilot` on three runs of the same query; that a row with a date and no rating means "we asked and there was nothing"; and that up to ten retailers are rated per run, so a fresh cache fills over a few days.

- [ ] **Step 2: Explain what moved and why**

In the confidence/detections section, say that ratings now come from the cache rather than the sweep, and why: Gemini was spending grounding passages reading Trustpilot because the prompt asked it to, and those passages are wanted for finding shops. Note the cost honestly — on a first run every retailer is unrated, so `rated_*` and `value_*` are blank until the cache fills, and a stored rating can be a month old.

- [ ] **Step 3: Document the settings**

`ratings_sheet` (default `Ratings`) and `rating_rate_d` (default 30).

- [ ] **Step 4: Verify the docs against the code**

```
python -c "from price_monitor.sheets.ratings_tab import COLUMNS; print(COLUMNS)"
python -c "from price_monitor.search.ratings import RATING_BATCH; print(RATING_BATCH)"
```
Both must match what the README claims.

- [ ] **Step 5: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add README.md
git commit -m "docs: document the cached ratings tab"
```

---

## Final verification

- [ ] `pytest tests/unit -q` — all pass
- [ ] `black --check src tests && isort --check src tests && flake8 src tests` — clean
- [ ] `pytest tests/integration --collect-only -q` — collects, no errors
- [ ] `grep -rn "rating_count\|rating_source" src/price_monitor/search/prompts.py` — returns nothing
- [ ] `python -c "from price_monitor.sheets.ratings_tab import COLUMNS; print(COLUMNS[-1])"` prints `checked`
- [ ] A live run remains outstanding. The premise — that grounding passages freed from Trustpilot get spent on shops — is a hypothesis, and only a real sweep can confirm whether `sources` rises.
