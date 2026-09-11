# Market Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, UK-wide market sweep alongside the existing per-supplier price lookup, recording the best price found with a computed confidence score that gates drop notifications.

**Architecture:** A new `MarketSearcher` mirrors the existing two-call (grounded search → un-grounded JSON) shape but asks for a *set* of retailer candidates rather than one price. A pure `confidence.score()` function grades the set on four multiplied factors. Market readings land in the existing history tab under a sentinel website key, so every existing statistic, baseline and drop band works on them unchanged.

**Tech Stack:** Python 3.11, pandas, pytest, llmbridge (Gemini), google-drive-api. Black + isort + flake8.

**Spec:** `docs/superpowers/specs/2026-09-11-market-search-design.md`

## Global Constraints

- Branch: `feat/market-search`. Do not merge to `master`.
- Python style per `~/.claude/CLAUDE.md`: PEP 604 unions (`X | None`, never `Optional`), no `from __future__ import annotations`, annotate every signature and dataclass field, `pathlib.Path` for paths, no `print()`, module docstring on every `.py` file.
- Comments only for non-obvious invariants, sign conventions or workarounds. Do not narrate what the code does.
- Strict KISS/YAGNI. No new dependencies. No abstract base classes, no dependency injection, no repositories.
- `@dataclass(frozen=True)` for value objects; plain `@dataclass` for config.
- Commit after every task. Run `black src tests && isort src tests && flake8 src tests` before each commit.
- Full suite must pass before each commit: `pytest tests/unit -q`.
- Constants that the spec names as module constants stay module constants. Only `market_min_confidence` becomes config.

### Two deliberate deviations from the spec

The spec places `Candidate` and `MARKET_KEY` in `search/market.py`. Both move, for import-graph reasons the spec did not account for:

1. **`Candidate` lives in `search/confidence.py`.** `market.py` imports `confidence`, and `score()` annotates `list[Candidate]`. Because this project does not use `from __future__ import annotations`, that annotation is evaluated at import time, so defining `Candidate` in `market.py` would be a circular import. `Candidate` is the scorer's input contract, so `confidence.py` is its natural home anyway.
2. **`MARKET_KEY` and `supplier_domain()` live in `models.py`.** Both sheet modules need `MARKET_KEY`, and `history_tab.py` plus `msg_forwarder.py` both need `supplier_domain()`. The established boundary is that `sheets/` knows nothing about `search/`, so neither can live in `market.py`. `models.py` is already the shared module, and already does URL parsing in `Item.is_direct_url`.
3. **`_FORMAT_PROVIDER` is renamed `FORMAT_PROVIDER`.** `market.py` needs it, and importing another module's underscore-private name is worse than widening it. It has no other references (`grep -rn "_FORMAT_PROVIDER" src tests` returns three lines, all inside `searcher.py`).

---

## File Structure

| File | Responsibility |
| --- | --- |
| `src/price_monitor/models.py` | MODIFY — `MARKET_KEY`, `supplier_domain()`, `Item.market`, `PriceReading.confidence`, `PriceReading.rating` |
| `src/price_monitor/app_config.py` | MODIFY — `PriceCtrl.market_min_confidence` |
| `src/price_monitor/search/validation.py` | MODIFY — extract `normalise_price()`, shared with the market path |
| `src/price_monitor/search/searcher.py` | MODIFY — lift `_retrying` to a module-level `retrying()` |
| `src/price_monitor/search/confidence.py` | CREATE — `Candidate` and the pure `score()` function |
| `src/price_monitor/search/prompts.py` | MODIFY — market search and market format templates |
| `src/price_monitor/search/market.py` | CREATE — `MarketSearcher`: two calls, candidate ranking, reputation floor |
| `src/price_monitor/sheets/history_tab.py` | MODIFY — `confidence`/`rating` columns, `market_summary()`, market-free `summarise()` |
| `src/price_monitor/sheets/items_tab.py` | MODIFY — `market` opt-in column, four new tool columns |
| `src/price_monitor/msgserver_client/msg_forwarder.py` | MODIFY — `format_market_drop()` |
| `src/price_monitor/price_monitor.py` | MODIFY — wire the sweep in, gate drops on confidence |
| `config/config.example.json` | MODIFY — `market_min_confidence` |
| `README.md` | MODIFY — the new columns and what confidence means |

---

## Task 1: Value objects

**Files:**
- Modify: `src/price_monitor/models.py`
- Test: `tests/unit/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `MARKET_KEY: str`, `supplier_domain(url: str) -> str`, `Item(name: str, website: str, market: bool = False)`, `PriceReading(..., confidence: float | None = None, rating: str = "")`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_models.py`:

```python
def test_item_defaults_to_not_market_tracked():
    assert Item(name="Widget", website="shop.example").market is False


def test_item_can_opt_into_market_tracking():
    assert Item(name="Widget", website="shop.example", market=True).market is True


def test_market_key_is_the_sentinel_website():
    from price_monitor.models import MARKET_KEY

    assert MARKET_KEY == "market"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.argos.co.uk/product/123", "argos.co.uk"),
        ("https://argos.co.uk/product/123", "argos.co.uk"),
        ("http://SHOP.Example/x", "shop.example"),
        ("", ""),
    ],
)
def test_supplier_domain_strips_scheme_and_www(url, expected):
    from price_monitor.models import supplier_domain

    assert supplier_domain(url) == expected


def test_reading_defaults_carry_no_confidence_or_rating():
    reading = PriceReading(
        timestamp=datetime(2026, 9, 11, 6, 0, 0),
        item="Widget",
        website="shop.example",
        price=10.0,
        currency="GBP",
        status=PriceStatus.OK,
    )
    assert reading.confidence is None
    assert reading.rating == ""
```

Add `PriceReading`, `PriceStatus` and `pytest` to the existing imports at the top of the file if they are not already there.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_models.py -q`
Expected: FAIL — `TypeError: Item.__init__() got an unexpected keyword argument 'market'` and `ImportError: cannot import name 'MARKET_KEY'`.

- [ ] **Step 3: Implement**

In `src/price_monitor/models.py`, after the imports:

```python
# The sentinel `website` under which a UK-wide market sweep is recorded, so its
# readings share the history tab with per-supplier ones while keeping their own
# (item, website) series: their own mean, baseline and drop bands.
MARKET_KEY = "market"
```

Add, below the `MARKET_KEY` definition:

```python
def supplier_domain(url: str) -> str:
    """The bare domain of ``url``, as the sheet's ``best_supplier`` value."""
    netloc = urlparse(url.strip()).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc
```

with `from urllib.parse import urlparse` added to the imports.

Add to `Item`:

```python
    market: bool = False
```

Add to `PriceReading`, after `status`:

```python
    confidence: float | None = None
    rating: str = ""
```

Note `source_url: str = ""` and `note: str = ""` already have defaults, so the new fields must be declared after them to keep the dataclass field order legal.

- [ ] **Step 4: Run the full unit suite**

Run: `pytest tests/unit -q`
Expected: PASS. Every existing construction of `Item` and `PriceReading` uses keywords or relies on defaults, so nothing else changes.

- [ ] **Step 5: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/models.py tests/unit/test_models.py
git commit -m "feat: add market opt-in, confidence and rating to the value objects"
```

---

## Task 2: Extract `normalise_price`

The market path must apply exactly the same currency, plausibility and VAT rules as the supplier path. This task extracts them **without changing behaviour**; the regression tests are written first and must pass both before and after.

**Files:**
- Modify: `src/price_monitor/search/validation.py`
- Test: `tests/unit/test_validation.py`

**Interfaces:**
- Consumes: `PriceCtrl`, `PriceStatus`.
- Produces: `normalise_price(payload: dict, ctrl: PriceCtrl) -> tuple[float | None, PriceStatus | None, str]`. On success returns `(price, None, note)` where `price` is VAT-inclusive and `note` is the VAT explanation or `""`. On failure returns `(None, status, note)` where `status` is the rejection reason.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_validation.py`:

```python
from price_monitor.search.validation import normalise_price


def _normalise(**overrides):
    payload = {"price": 100.0, "currency": "GBP"}
    payload.update(overrides)
    return normalise_price(payload, CTRL)


def test_normalise_accepts_a_clean_price():
    price, failure, note = _normalise()
    assert (price, failure, note) == (100.0, None, "")


def test_normalise_rejects_a_foreign_currency():
    price, failure, note = _normalise(currency="USD")
    assert price is None
    assert failure == PriceStatus.WRONG_CURRENCY
    assert note == "reported USD"


def test_normalise_rejects_a_boolean_price():
    assert _normalise(price=True)[1] == PriceStatus.REJECTED


def test_normalise_rejects_a_non_numeric_price():
    assert _normalise(price="cheap")[1] == PriceStatus.REJECTED


def test_normalise_rejects_an_implausible_price():
    assert _normalise(price=CTRL.max_plausible_price + 1)[1] == PriceStatus.REJECTED


def test_normalise_adds_vat_when_the_price_excludes_it():
    price, failure, note = _normalise(price=100.0, vat_included=False)
    assert price == 120.0
    assert failure is None
    assert note == "ex-VAT 100 +20% VAT"


def test_normalise_assumes_vat_is_included_when_unstated():
    assert _normalise(price=100.0)[0] == 100.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_validation.py -q`
Expected: FAIL — `ImportError: cannot import name 'normalise_price'`.

- [ ] **Step 3: Implement the extraction**

In `src/price_monitor/search/validation.py`, add above `validate()`:

```python
def normalise_price(
    payload: dict, ctrl: PriceCtrl
) -> tuple[float | None, PriceStatus | None, str]:
    """Coerce a reported price to a VAT-inclusive number, or say why not.

    Returns ``(price, None, note)`` on success and ``(None, status, note)`` on
    rejection. Shared by the single-supplier ladder and the market sweep so that
    a market row and a supplier row for the same product stay comparable — if
    only one path added VAT, every comparison the sheet invites would be wrong.
    """
    currency = str(payload.get("currency") or "").strip().upper()
    if currency != ctrl.currency.strip().upper():
        return None, PriceStatus.WRONG_CURRENCY, f"reported {currency or '?'}"

    if isinstance(payload.get("price"), bool):
        return None, PriceStatus.REJECTED, "price was a boolean"

    try:
        price = float(payload.get("price"))
    except (TypeError, ValueError):
        return None, PriceStatus.REJECTED, "price was not a number"

    if math.isnan(price) or math.isinf(price):
        return None, PriceStatus.REJECTED, f"price was {price}"

    note = ""
    # Recorded history is always VAT-inclusive. Absent vat_included, assume the
    # price already includes VAT — adding tax to an inclusive price would invent
    # a jump that never happened, which is the worse of the two errors.
    if payload.get("vat_included", True) is False:
        ex_vat = price
        price = round(price * (1 + ctrl.vat_rate), 2)
        note = f"ex-VAT {ex_vat:g} +{ctrl.vat_rate:.0%} VAT"

    if price <= 0 or price > ctrl.max_plausible_price:
        return None, PriceStatus.REJECTED, f"implausible price {price}"

    return price, None, note
```

Now replace the body of `validate()` between the `found` check and the `last_price` check with:

```python
    price, failure, extra = normalise_price(payload, ctrl)
    if failure is not None:
        return reading(None, failure, extra)
    if extra:
        note = f"{note}; {extra}" if note else extra
```

Delete the lines that moved: the currency check, the boolean check, the `float()` conversion, the NaN/inf check, the VAT block and the plausibility check. Keep the `found` check above and the `last_price` movement check below exactly as they are.

- [ ] **Step 4: Run the full validation suite**

Run: `pytest tests/unit/test_validation.py -q`
Expected: PASS — both the new tests and every pre-existing one. Any pre-existing failure means the extraction changed behaviour; fix the extraction, never the old test.

- [ ] **Step 5: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/search/validation.py tests/unit/test_validation.py
git commit -m "refactor: extract normalise_price for reuse by the market sweep"
```

---

## Task 3: Lift `retrying` to module level

Another pure refactor. The proof it is mechanical is that `tests/unit/test_searcher.py` passes untouched.

**Files:**
- Modify: `src/price_monitor/search/searcher.py`
- Test: `tests/unit/test_searcher.py` (unchanged — do not edit it)

**Interfaces:**
- Consumes: `LLMError`, `PromptResponse`.
- Produces: `retrying(call: Callable[[], PromptResponse], label: str, logger: logging.Logger) -> PromptResponse`, and `FORMAT_PROVIDER: str` (renamed from `_FORMAT_PROVIDER`).

- [ ] **Step 1: Move the method**

In `src/price_monitor/search/searcher.py`, replace the `_retrying` **method** with this module-level function, placed just below `_is_retryable`:

```python
def retrying(
    call: Callable[[], PromptResponse], label: str, logger: logging.Logger
) -> PromptResponse:
    """Run ``call``, retrying transient failures with exponential backoff.

    The final attempt re-raises rather than swallowing, so the caller maps the
    failure to a status itself.
    """
    delay = _RETRY_BASE_DELAY_S
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return call()
        except Exception as exc:
            if attempt == _RETRY_ATTEMPTS or not _is_retryable(exc):
                raise
            logger.warning(
                f"Transient {label} failure "
                f"(attempt {attempt}/{_RETRY_ATTEMPTS}), "
                f"retrying in {delay:.0f}s: {exc}"
            )
            time.sleep(delay)
            delay *= 2
    raise LLMError(f"{label} call exhausted retries")  # unreachable
```

- [ ] **Step 2: Widen the provider constant**

`market.py` needs the un-grounded provider name too, and importing another
module's underscore-private name is worse than widening it. Rename
`_FORMAT_PROVIDER` to `FORMAT_PROVIDER` and update its two uses inside
`searcher.py` (the `search_provider` ternary and the `format_client`
construction). `grep -rn "_FORMAT_PROVIDER" src tests` must return nothing
afterwards.

- [ ] **Step 3: Update the two call sites**

In `PriceSearcher.price()`, change both calls:

```python
            searched = retrying(
                lambda: self._search_client.prompt(search_prompt(item)),
                "search",
                self.logger,
            )
```

```python
            formatted = retrying(
                lambda: self._format_client.prompt(format_prompt(searched.text, urls)),
                "format",
                self.logger,
            )
```

- [ ] **Step 4: Run the searcher suite unchanged**

Run: `pytest tests/unit/test_searcher.py -q`
Expected: PASS, with no edits to the test file. If a test referenced `PriceSearcher._retrying` directly it will fail — in that case update only that reference, and note it in the commit message.

- [ ] **Step 5: Run the full suite**

Run: `pytest tests/unit -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/search/searcher.py
git commit -m "refactor: lift the retry loop to a module-level function"
```

---

## Task 4: Confidence scoring

The heart of the feature, and the only part with no I/O. Build it in isolation with a table-driven test.

**Files:**
- Create: `src/price_monitor/search/confidence.py`
- Test: `tests/unit/test_confidence.py`

**Interfaces:**
- Consumes: nothing from this project.
- Produces:
  - `Candidate(supplier: str, price: float, url: str, in_stock: bool, rating: float | None = None, rating_count: int | None = None, rating_source: str = "")`, with properties `rated: bool` and `rating_label: str`.
  - `score(candidates: list[Candidate], cited_urls: list[str]) -> tuple[float, str]`.
  - `MIN_RATING_COUNT: int` (50) and `FLOOR_RATING: float` (3.0), imported by `market.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_confidence.py`:

```python
"""Unit tests for the market-sweep confidence score."""

import pytest

from price_monitor.search.confidence import Candidate, score

CITED = "https://argos.co.uk/widget"


def _candidate(price: float, **overrides) -> Candidate:
    fields = {
        "supplier": "Argos",
        "price": price,
        "url": CITED,
        "in_stock": True,
        "rating": 4.4,
        "rating_count": 900,
        "rating_source": "trustpilot",
    }
    fields.update(overrides)
    return Candidate(**fields)


def _clustered(n: int, **winner_overrides) -> list[Candidate]:
    """A winner at 100 plus n-1 others at 105, 110, 115 ... (ratio ~0.91)."""
    others = [_candidate(105.0 + 5 * i, url=f"https://other{i}.co.uk/w") for i in range(n - 1)]
    return [_candidate(100.0, **winner_overrides)] + others


@pytest.mark.parametrize(
    "candidates, expected",
    [
        (_clustered(4), 1.00),
        (_clustered(4, rating=None, rating_count=None), 0.80),
        (_clustered(4, rating=3.2, rating_count=900), 0.44),
        (_clustered(2, rating=None, rating_count=None), 0.53),
        (_clustered(1), 0.33),
    ],
)
def test_scores_match_the_specified_worked_examples(candidates, expected):
    assert score(candidates, [CITED])[0] == expected


def test_a_winner_well_under_the_others_median_is_punished():
    # Winner 100 against others at 200/220/240 -> median 220, ratio 100/220 =
    # 0.4545. The ratio is never rounded: the outlier factor is a continuous
    # slide, and only the final product is rounded, once.
    others = [_candidate(p, url=f"https://o{p}.co.uk/w") for p in (200.0, 220.0, 240.0)]
    assert score([_candidate(100.0)] + others, [CITED])[0] == 0.14


def test_a_modest_gap_under_the_median_is_only_partly_punished():
    # Winner 100 against others at 140/143/146 -> median 143, ratio 0.70.
    others = [_candidate(p, url=f"https://o{p}.co.uk/w") for p in (140.0, 143.0, 146.0)]
    assert score([_candidate(100.0)] + others, [CITED])[0] == 0.75


def test_a_sole_source_is_not_punished_twice():
    """n=1 has nothing to compare against, so only corroboration penalises it."""
    assert score([_candidate(100.0)], [CITED])[0] == 0.33


def test_an_uncited_url_scores_below_a_cited_one():
    cited = score(_clustered(4), [CITED])[0]
    uncited = score(_clustered(4), ["https://elsewhere.example/x"])[0]
    assert uncited < cited
    assert uncited == 0.6


def test_a_winner_with_no_url_scores_lowest_on_sourcing():
    assert score(_clustered(4, url=""), [CITED])[0] == 0.3


def test_the_reputation_shoulder_is_flat_at_and_above_four():
    assert score(_clustered(4, rating=4.0), [CITED])[0] == 1.00
    assert score(_clustered(4, rating=4.9), [CITED])[0] == 1.00


def test_the_reputation_factor_bottoms_out_at_three():
    assert score(_clustered(4, rating=3.0), [CITED])[0] == 0.30


def test_a_rating_on_too_few_reviews_reads_as_unknown():
    """4.9 from 12 reviews is noise, and must not score better than unrated."""
    thin = score(_clustered(4, rating=4.9, rating_count=12), [CITED])[0]
    unrated = score(_clustered(4, rating=None, rating_count=None), [CITED])[0]
    assert thin == unrated == 0.80


def test_the_reason_names_every_factor():
    _, reason = score(_clustered(4), [CITED])
    assert "4 sources" in reason
    assert "cited" in reason
    assert "rated 4.4" in reason


def test_an_empty_candidate_set_scores_zero():
    assert score([], [CITED]) == (0.0, "no candidates")


def test_rating_label_renders_for_the_sheet():
    assert _candidate(100.0).rating_label == "4.4 Trustpilot"
    assert _candidate(100.0, rating=None, rating_count=None).rating_label == ""
    assert _candidate(100.0, rating=4.4, rating_count=12).rating_label == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_confidence.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'price_monitor.search.confidence'`.

- [ ] **Step 3: Implement**

Create `src/price_monitor/search/confidence.py`:

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/test_confidence.py -q`
Expected: PASS, all cases.

- [ ] **Step 5: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/search/confidence.py tests/unit/test_confidence.py
git commit -m "feat: score market-sweep confidence from candidate agreement"
```

---

## Task 5: Market prompts

**Files:**
- Modify: `src/price_monitor/search/prompts.py`
- Test: `tests/unit/test_prompts.py`

**Interfaces:**
- Consumes: `Item`.
- Produces: `market_prompt(item: Item) -> str`, `market_format_prompt(report: str, urls: list[str]) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_prompts.py`:

```python
from price_monitor.search.prompts import market_format_prompt, market_prompt


def test_market_prompt_names_the_product_and_asks_for_a_set():
    prompt = market_prompt(Item(name="Widget 3000", website="shop.example"))
    assert "Widget 3000" in prompt
    assert "three to six" in prompt


def test_market_prompt_permits_a_single_retailer():
    """Without this the model pads the list, which corrupts the agreement score."""
    prompt = market_prompt(Item(name="Widget", website="shop.example"))
    assert "only one retailer" in prompt


def test_market_prompt_forbids_estimating_a_rating():
    prompt = market_prompt(Item(name="Widget", website="shop.example"))
    assert "Trustpilot" in prompt
    assert "do not estimate one" in prompt


def test_market_prompt_excludes_the_tracked_site_from_its_wording():
    """The sweep is market-wide; the item's own website is not part of it."""
    assert "shop.example" not in market_prompt(
        Item(name="Widget", website="shop.example")
    )


def test_market_format_prompt_carries_the_report_and_urls():
    prompt = market_format_prompt("Argos has it at 99.99", ["https://argos.co.uk/w"])
    assert "Argos has it at 99.99" in prompt
    assert "https://argos.co.uk/w" in prompt
    assert "candidates" in prompt


def test_market_format_prompt_renders_no_urls_as_none():
    assert "none" in market_format_prompt("report", [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_prompts.py -q`
Expected: FAIL — `ImportError: cannot import name 'market_prompt'`.

- [ ] **Step 3: Implement**

Add to `src/price_monitor/search/prompts.py`:

```python
def market_prompt(item: Item) -> str:
    """Build the call-one prompt for a UK-wide sweep across retailers."""
    return _MARKET_TEMPLATE.format(item=item.name)


def market_format_prompt(report: str, urls: list[str]) -> str:
    """Build the call-two prompt that turns a sweep report into JSON."""
    return _MARKET_FORMAT_TEMPLATE.format(
        urls=", ".join(urls) if urls else "none", report=report
    )
```

And the templates, beside the existing ones:

```python
_MARKET_TEMPLATE = """Find the current UK price of this product across retailers.

Product: {item}

Find current UK prices for this product from as many different UK retailers as
you can — aim for three to six. For each retailer report:
- the retailer's name
- the price shown right now, with its currency
- whether that price includes VAT (sales tax)
- whether it is in stock
- the exact URL of the page you read the price from

For each retailer also report its Trustpilot rating out of 5 and how many
reviews that rating is based on. If the retailer has no Trustpilot page, use its
Google review rating instead and say which of the two you used. If you cannot
find a rating, say unknown — do not estimate one.

Only list the same product — the same model and variant. Do not list
accessories, bundles, multipacks, refurbished or used items unless the product
name asks for one. Do not guess a price; list only prices you actually saw. If
you can find it at only one retailer, list only that one."""

_MARKET_FORMAT_TEMPLATE = """Convert the report below into one JSON object and nothing else.

Schema:
{{
  "candidates": [
    {{
      "supplier": "<retailer name>",
      "price": <number>,
      "currency": "<ISO 4217 code>",
      "url": "<string>",
      "in_stock": <true or false>,
      "vat_included": <true or false>,
      "rating": <number or null>,
      "rating_count": <integer or null>,
      "rating_source": "trustpilot" | "google" | ""
    }}
  ],
  "note": "<string>"
}}

Rules:
- One entry per retailer the report names with a price. If the report gives no
  price at any retailer, return an empty candidates list and say why in note.
- vat_included records whether that retailer's price already includes VAT. When
  the report does not say, set it to true so no tax is added to a price that may
  already include it. Never adjust a price yourself.
- price is a plain number: no currency symbol, no thousands separators.
- rating and rating_count are null when the report gives no rating for that
  retailer. Never invent a rating.
- rating_source is "trustpilot" or "google" — whichever the report used — and ""
  when there is no rating.
- Output the JSON object only. No markdown fences, no commentary.

Known source URLs: {urls}

Report:
{report}"""
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/test_prompts.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/search/prompts.py tests/unit/test_prompts.py
git commit -m "feat: add the market sweep prompt templates"
```

---

## Task 6: The MarketSearcher

**Files:**
- Create: `src/price_monitor/search/market.py`
- Test: `tests/unit/test_market.py`

**Interfaces:**
- Consumes: `retrying` and `FORMAT_PROVIDER` (Task 3), `normalise_price` (Task 2), `Candidate`/`score`/`MIN_RATING_COUNT`/`FLOOR_RATING` (Task 4), `market_prompt`/`market_format_prompt` (Task 5), `MARKET_KEY` (Task 1), and the existing `extract_json`, `grounding_urls`.
- Produces:
  - `MarketSearcher(config: LLMConfig, ctrl: PriceCtrl, logger: logging.Logger, search_client: LLMClient | None = None, format_client: LLMClient | None = None)`
  - `MarketSearcher.price_market(item: Item, last_price: float | None, timestamp: datetime) -> PriceReading`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_market.py`:

```python
"""Unit tests for the two-call market sweep orchestration."""

import json
import logging
from datetime import datetime

import pytest
from llmbridge.exceptions import LLMConnectionError
from llmbridge.models import PromptResponse

from price_monitor.app_config import LLMConfig, PriceCtrl
from price_monitor.models import MARKET_KEY, Item, PriceStatus
from price_monitor.search.market import MarketSearcher

NOW = datetime(2026, 9, 11, 6, 0, 0)
ITEM = Item(name="Widget", website="shop.example", market=True)


def _response(text: str, raw: dict | None = None) -> PromptResponse:
    return PromptResponse(
        text=text,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        model="gemini-3.7-flash",
        provider="gemini",
        latency_ms=1.0,
        raw_response=raw or {},
    )


class FakeClient:
    """Records prompts and replays queued responses or raises queued errors."""

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


def _candidate(**overrides) -> dict:
    raw = {
        "supplier": "Argos",
        "price": 100.0,
        "currency": "GBP",
        "url": "https://argos.co.uk/widget",
        "in_stock": True,
        "vat_included": True,
        "rating": 4.4,
        "rating_count": 900,
        "rating_source": "trustpilot",
    }
    raw.update(overrides)
    return raw


def _searcher(logger, candidates, note="") -> MarketSearcher:
    payload = json.dumps({"candidates": candidates, "note": note})
    return MarketSearcher(
        LLMConfig(api_key="k"),
        PriceCtrl(),
        logger,
        search_client=FakeClient(_response("a prose report")),
        format_client=FakeClient(_response(payload)),
    )


def test_the_cheapest_in_stock_candidate_wins(logger):
    searcher = _searcher(
        logger,
        [
            _candidate(price=120.0, url="https://a.co.uk/w"),
            _candidate(price=100.0, url="https://b.co.uk/w"),
            _candidate(price=110.0, url="https://c.co.uk/w"),
        ],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.status == PriceStatus.OK
    assert reading.price == 100.0
    assert reading.website == MARKET_KEY
    assert reading.item == "Widget"
    assert reading.source_url == "https://b.co.uk/w"
    assert reading.rating == "4.4 Trustpilot"
    assert reading.confidence is not None


def test_out_of_stock_candidates_are_not_eligible_to_win(logger):
    searcher = _searcher(
        logger,
        [
            _candidate(price=50.0, in_stock=False, url="https://a.co.uk/w"),
            _candidate(price=100.0, url="https://b.co.uk/w"),
        ],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.price == 100.0


def test_a_badly_rated_retailer_is_dropped_by_the_floor(logger):
    searcher = _searcher(
        logger,
        [
            _candidate(price=50.0, rating=2.1, rating_count=900, url="https://a.co.uk/w"),
            _candidate(price=100.0, url="https://b.co.uk/w"),
        ],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.price == 100.0


def test_a_bad_rating_on_too_few_reviews_does_not_trigger_the_floor(logger):
    """2.1 from 12 reviews is noise, not evidence — the candidate survives."""
    searcher = _searcher(
        logger,
        [
            _candidate(price=50.0, rating=2.1, rating_count=12, url="https://a.co.uk/w"),
            _candidate(price=100.0, url="https://b.co.uk/w"),
        ],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.price == 50.0


def test_an_unrated_retailer_survives_the_floor(logger):
    """Dropping unknowns would bias every sweep toward the big chains."""
    searcher = _searcher(
        logger,
        [
            _candidate(price=50.0, rating=None, rating_count=None, url="https://a.co.uk/w"),
        ],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.price == 50.0
    assert reading.rating == ""


def test_a_foreign_currency_candidate_is_dropped(logger):
    searcher = _searcher(
        logger,
        [
            _candidate(price=50.0, currency="USD", url="https://a.co.uk/w"),
            _candidate(price=100.0, url="https://b.co.uk/w"),
        ],
    )
    assert searcher.price_market(ITEM, None, NOW).price == 100.0


def test_an_ex_vat_candidate_is_grossed_up_before_ranking(logger):
    """90 ex-VAT is 108 inc-VAT, so the 100 inc-VAT candidate is cheaper."""
    searcher = _searcher(
        logger,
        [
            _candidate(price=90.0, vat_included=False, url="https://a.co.uk/w"),
            _candidate(price=100.0, url="https://b.co.uk/w"),
        ],
    )
    assert searcher.price_market(ITEM, None, NOW).price == 100.0


def test_no_candidates_is_not_found(logger):
    reading = _searcher(logger, [], note="nobody stocks it").price_market(ITEM, None, NOW)
    assert reading.status == PriceStatus.NOT_FOUND
    assert reading.price is None
    assert reading.website == MARKET_KEY


def test_all_candidates_out_of_stock_is_not_found(logger):
    searcher = _searcher(logger, [_candidate(in_stock=False)])
    assert searcher.price_market(ITEM, None, NOW).status == PriceStatus.NOT_FOUND


def test_a_big_move_from_the_last_market_price_is_suspect(logger):
    searcher = _searcher(logger, [_candidate(price=100.0)])
    reading = searcher.price_market(ITEM, 400.0, NOW)
    assert reading.status == PriceStatus.SUSPECT
    assert reading.price == 100.0


def test_unparseable_json_is_a_parse_error(logger):
    searcher = MarketSearcher(
        LLMConfig(api_key="k"),
        PriceCtrl(),
        logger,
        search_client=FakeClient(_response("report")),
        format_client=FakeClient(_response("not json at all")),
    )
    assert searcher.price_market(ITEM, None, NOW).status == PriceStatus.PARSE_ERROR


def test_an_empty_search_reply_is_a_parse_error(logger):
    searcher = MarketSearcher(
        LLMConfig(api_key="k"),
        PriceCtrl(),
        logger,
        search_client=FakeClient(_response("   ")),
        format_client=FakeClient(_response("{}")),
    )
    assert searcher.price_market(ITEM, None, NOW).status == PriceStatus.PARSE_ERROR


def test_a_raising_client_is_an_error_not_an_exception(logger):
    searcher = MarketSearcher(
        LLMConfig(api_key="k"),
        PriceCtrl(),
        logger,
        search_client=FakeClient(LLMConnectionError("down"), LLMConnectionError("down"),
                                 LLMConnectionError("down")),
        format_client=FakeClient(_response("{}")),
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.status == PriceStatus.ERROR
    assert reading.website == MARKET_KEY

```

Note: the raising-client test queues three errors because `retrying` makes three attempts. `LLMConnectionError` is retryable, so all three are consumed.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_market.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'price_monitor.search.market'`.

- [ ] **Step 3: Implement**

Create `src/price_monitor/search/market.py`:

```python
"""UK-wide market sweep.

Asks a search-grounded model what an item costs across UK retailers generally,
ranks the answers, and records the cheapest one that survives validation with a
confidence score describing how much the sweep supports it. Mirrors
:class:`~price_monitor.search.searcher.PriceSearcher`'s two-call shape for the
same reason: grounding and structured output are unreliable together. Nothing
raises out of :meth:`price_market`.
"""

import logging
from datetime import datetime

from llmbridge import LLMClient
from llmbridge.exceptions import LLMError
from py_utils.logger import get_child_logger

from price_monitor.app_config import LLMConfig, PriceCtrl
from price_monitor.models import MARKET_KEY, Item, PriceReading, PriceStatus
from price_monitor.search.confidence import (
    FLOOR_RATING,
    MIN_RATING_COUNT,
    Candidate,
    score,
)
from price_monitor.search.gemini_search import grounding_urls
from price_monitor.search.json_extract import extract_json
from price_monitor.search.prompts import market_format_prompt, market_prompt
from price_monitor.search.searcher import FORMAT_PROVIDER, retrying
from price_monitor.search.validation import normalise_price


class MarketSearcher:
    """Prices a single :class:`Item` across UK retailers rather than at one."""

    def __init__(
        self,
        config: LLMConfig,
        ctrl: PriceCtrl,
        logger: logging.Logger,
        search_client: LLMClient | None = None,
        format_client: LLMClient | None = None,
    ) -> None:
        self.logger = get_child_logger(logger, __class__.__name__)
        self._ctrl = ctrl
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

    def price_market(
        self, item: Item, last_price: float | None, timestamp: datetime
    ) -> PriceReading:
        """Sweep UK retailers for ``item``, never raising on failure."""
        try:
            searched = retrying(
                lambda: self._search_client.prompt(market_prompt(item)),
                "market search",
                self.logger,
            )
        except (LLMError, KeyError, IndexError, TypeError) as exc:
            # Same unguarded parse_response() exposure as the supplier path: a
            # safety-filtered or truncated reply raises out of prompt() rather
            # than arriving as an LLMError.
            self.logger.warning(f"Market search failed for '{item.name}': {exc}")
            return self._failed(item, timestamp, PriceStatus.ERROR, str(exc))

        if not searched.text or not searched.text.strip():
            self.logger.warning(f"Market search returned no text for '{item.name}'")
            return self._failed(
                item, timestamp, PriceStatus.PARSE_ERROR, "empty search reply"
            )

        urls = grounding_urls(searched.raw_response)

        try:
            formatted = retrying(
                lambda: self._format_client.prompt(
                    market_format_prompt(searched.text, urls)
                ),
                "market format",
                self.logger,
            )
        except (LLMError, KeyError, IndexError, TypeError) as exc:
            self.logger.warning(f"Market format failed for '{item.name}': {exc}")
            return self._failed(item, timestamp, PriceStatus.ERROR, str(exc))

        try:
            payload = extract_json(formatted.text)
        except ValueError as exc:
            self.logger.warning(f"Unparseable market reply for '{item.name}': {exc}")
            return self._failed(item, timestamp, PriceStatus.PARSE_ERROR, str(exc))

        return self._rank(payload, item, timestamp, last_price, urls)

    def _rank(
        self,
        payload: dict,
        item: Item,
        timestamp: datetime,
        last_price: float | None,
        urls: list[str],
    ) -> PriceReading:
        candidates = self._candidates(payload)
        if not candidates:
            note = str(payload.get("note") or "").strip() or "no usable candidates"
            return self._failed(item, timestamp, PriceStatus.NOT_FOUND, note)

        winner = min(candidates, key=lambda c: c.price)
        value, reason = score(candidates, urls)

        status = PriceStatus.OK
        note = reason
        if last_price is not None and last_price > 0:
            move = abs(winner.price - last_price) / last_price
            if move > self._ctrl.suspect_threshold:
                status = PriceStatus.SUSPECT
                note = f"{note}; moved {move:.0%} from {last_price}"

        return PriceReading(
            timestamp=timestamp,
            item=item.name,
            website=MARKET_KEY,
            price=winner.price,
            currency=self._ctrl.currency,
            status=status,
            source_url=winner.url,
            note=note,
            confidence=value,
            rating=winner.rating_label,
        )

    def _candidates(self, payload: dict) -> list[Candidate]:
        """Validated, in-stock, above-floor candidates, VAT-normalised."""
        candidates: list[Candidate] = []
        for raw in payload.get("candidates") or []:
            if not isinstance(raw, dict):
                continue

            price, failure, _ = normalise_price(raw, self._ctrl)
            if failure is not None or price is None:
                continue
            if raw.get("in_stock", True) is False:
                continue

            rating = _as_float(raw.get("rating"))
            count = _as_int(raw.get("rating_count"))
            candidate = Candidate(
                supplier=str(raw.get("supplier") or "").strip(),
                price=price,
                url=str(raw.get("url") or "").strip(),
                in_stock=True,
                rating=rating,
                rating_count=count,
                rating_source=str(raw.get("rating_source") or "").strip(),
            )
            # The floor needs both halves: a bad score on a handful of reviews
            # is noise. An unrated retailer is never dropped here — plenty of
            # legitimate specialists have no Trustpilot presence, and dropping
            # them would bias every sweep toward the big chains.
            if candidate.rated and candidate.rating < FLOOR_RATING:
                self.logger.info(
                    f"Dropping {candidate.supplier}: rated {candidate.rating} "
                    f"on {candidate.rating_count} reviews"
                )
                continue
            candidates.append(candidate)
        return candidates

    def _failed(
        self, item: Item, timestamp: datetime, status: PriceStatus, note: str
    ) -> PriceReading:
        return PriceReading(
            timestamp=timestamp,
            item=item.name,
            website=MARKET_KEY,
            price=None,
            currency=self._ctrl.currency,
            status=status,
            note=note,
        )


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

`MIN_RATING_COUNT` is imported but used only via `Candidate.rated`; if flake8 flags it as unused, remove it from the import list.

The `suspect` check is written out here rather than shared with `validation.validate()`. It is five lines, and the two paths compose their notes differently — sharing it would cost more indirection than it saves.

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/test_market.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/search/market.py tests/unit/test_market.py
git commit -m "feat: sweep UK retailers and rank the candidates"
```

---

## Task 7: History tab

**Files:**
- Modify: `src/price_monitor/sheets/history_tab.py`
- Test: `tests/unit/test_history_tab.py`

**Interfaces:**
- Consumes: `MARKET_KEY` and `supplier_domain` (both from `models.py`, Task 1).
- Produces: `HistoryTab.market_summary() -> pd.DataFrame` with columns `MARKET_SUMMARY_COLUMNS = ["item", "best_price", "best_supplier", "best_supplier_rating", "confidence", "market_checked"]`; `COLUMNS` extended with `confidence` and `rating` before `status`; `summarise()` now excludes market rows.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_history_tab.py`, following that file's existing fake-sheet fixture:

```python
def test_confidence_and_rating_round_trip(history, gsheet):
    history.append(
        [
            PriceReading(
                timestamp=datetime(2026, 9, 11, 6, 0, 0),
                item="Widget",
                website=MARKET_KEY,
                price=100.0,
                currency="GBP",
                status=PriceStatus.OK,
                source_url="https://argos.co.uk/w",
                note="4 sources, cited",
                confidence=0.82,
                rating="4.1 Trustpilot",
            )
        ]
    )
    frame = history.read()
    assert list(frame.columns)[-1] == "status"
    assert frame["confidence"].iloc[0] == "0.82"
    assert frame["rating"].iloc[0] == "4.1 Trustpilot"


def test_supplier_rows_leave_confidence_and_rating_blank(history):
    history.append(
        [
            PriceReading(
                timestamp=datetime(2026, 9, 11, 6, 0, 0),
                item="Widget",
                website="shop.example",
                price=100.0,
                currency="GBP",
                status=PriceStatus.OK,
            )
        ]
    )
    frame = history.read()
    assert frame["confidence"].iloc[0] == ""
    assert frame["rating"].iloc[0] == ""


def test_summarise_ignores_market_rows(history):
    """The Items tab's current/min/mean describe the tracked supplier only."""
    history.append(
        [
            _reading("Widget", "shop.example", 200.0),
            _reading("Widget", MARKET_KEY, 100.0),
        ]
    )
    summary = history.summarise()
    assert list(summary["website"]) == ["shop.example"]
    assert summary["current"].iloc[0] == 200.0


def test_market_summary_reports_the_latest_sweep(history):
    history.append(
        [
            _reading("Widget", MARKET_KEY, 120.0, confidence=0.7, rating="4.0 Trustpilot"),
            _reading("Widget", MARKET_KEY, 100.0, confidence=0.9, rating="4.4 Trustpilot"),
        ]
    )
    row = history.market_summary().iloc[0]
    assert row["item"] == "Widget"
    assert row["best_price"] == 100.0
    assert row["best_supplier"] == "argos.co.uk"
    assert row["best_supplier_rating"] == "4.4 Trustpilot"
    assert row["confidence"] == "0.9"


def test_market_summary_is_empty_without_market_rows(history):
    history.append([_reading("Widget", "shop.example", 100.0)])
    assert history.market_summary().empty


def test_market_checked_moves_even_when_a_sweep_finds_nothing(history):
    """A run of failed sweeps must not look like a healthy stale price."""
    history.append([_reading("Widget", MARKET_KEY, 100.0)])
    history.append(
        [
            PriceReading(
                timestamp=datetime(2026, 9, 12, 6, 0, 0),
                item="Widget",
                website=MARKET_KEY,
                price=None,
                currency="GBP",
                status=PriceStatus.NOT_FOUND,
            )
        ]
    )
    row = history.market_summary().iloc[0]
    assert row["best_price"] == 100.0
    assert row["market_checked"] == "2026-09-12 06:00:00"
```

Add this helper near the top of the test file, and import `MARKET_KEY`:

```python
def _reading(item, website, price, confidence=None, rating=""):
    return PriceReading(
        timestamp=datetime(2026, 9, 11, 6, 0, 0),
        item=item,
        website=website,
        price=price,
        currency="GBP",
        status=PriceStatus.OK,
        source_url="https://www.argos.co.uk/w",
        confidence=confidence,
        rating=rating,
    )
```

If the existing file already defines a reading helper, extend that one instead of adding a second.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_history_tab.py -q`
Expected: FAIL — `KeyError: 'confidence'` and `AttributeError: 'HistoryTab' object has no attribute 'market_summary'`.

- [ ] **Step 3: Implement**

In `src/price_monitor/sheets/history_tab.py`:

Extend `COLUMNS`, keeping `status` last and its existing comment intact:

```python
COLUMNS = [
    "timestamp",
    "item",
    "website",
    "price",
    "currency",
    "source_url",
    "note",
    "confidence",
    "rating",
    "status",
]

MARKET_SUMMARY_COLUMNS = [
    "item",
    "best_price",
    "best_supplier",
    "best_supplier_rating",
    "confidence",
    "market_checked",
]
```

Add the imports:

```python
from price_monitor.models import (
    MARKET_KEY,
    PriceReading,
    PriceStats,
    supplier_domain,
)
```

`history_tab.py` must not import from `search/` — the sheet modules know nothing
about the LLM layer, which is why both of these live in `models.py`.

Extend `_to_row`:

```python
        "confidence": "" if reading.confidence is None else f"{reading.confidence:g}",
        "rating": reading.rating,
```

placed before `"status"`.

Make `summarise()` skip market rows by inserting this immediately after its `if frame.empty` guard:

```python
        # Market rows are summarised separately: current/min/mean on the Items
        # tab describe the tracked supplier, not the market.
        frame = frame[frame["website"] != MARKET_KEY]
        if frame.empty:
            return pd.DataFrame(columns=SUMMARY_COLUMNS)
```

Add `market_summary()`:

```python
    def market_summary(self) -> pd.DataFrame:
        """Latest market sweep per item, for the Items tab's market columns.

        ``best_price`` is the most recent *priced* sweep, while
        ``market_checked`` moves on every sweep of any status — so a run of
        failures shows as a moving timestamp beside a stale price rather than
        looking healthy.
        """
        frame = self._typed()
        if frame.empty:
            return pd.DataFrame(columns=MARKET_SUMMARY_COLUMNS)

        market = frame[frame["website"] == MARKET_KEY]
        if market.empty:
            return pd.DataFrame(columns=MARKET_SUMMARY_COLUMNS)

        rows = []
        for item, group in market.groupby("item", sort=False):
            ordered = group.sort_values("timestamp", kind="stable")
            priced = ordered[ordered["price"].notna()]
            latest = priced.iloc[-1] if not priced.empty else None
            rows.append(
                {
                    "item": item,
                    "best_price": float(latest["price"]) if latest is not None else "",
                    "best_supplier": (
                        supplier_domain(str(latest["source_url"]))
                        if latest is not None
                        else ""
                    ),
                    "best_supplier_rating": (
                        str(latest["rating"]) if latest is not None else ""
                    ),
                    "confidence": str(latest["confidence"]) if latest is not None else "",
                    "market_checked": ordered["timestamp"].iloc[-1],
                }
            )
        return pd.DataFrame(rows, columns=MARKET_SUMMARY_COLUMNS)
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/test_history_tab.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `pytest tests/unit -q`
Expected: PASS. Existing history tests that assert on column order or row width will need their expectations widened by two columns — update those expectations, not the production code.

- [ ] **Step 6: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/sheets/history_tab.py tests/unit/test_history_tab.py
git commit -m "feat: record confidence and rating, and summarise market sweeps"
```

---

## Task 8: Items tab

**Files:**
- Modify: `src/price_monitor/sheets/items_tab.py`
- Test: `tests/unit/test_items_tab.py`

**Interfaces:**
- Consumes: `MARKET_KEY`, `Item`, `HistoryTab.market_summary()`'s frame shape.
- Produces: `ItemsTab.write_summary(items: list[Item], summary: pd.DataFrame, market: pd.DataFrame) -> None` — **note the new third parameter**; `ItemsTab.read()` returning `Item`s with `market` populated.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_items_tab.py`:

```python
@pytest.mark.parametrize("cell", ["yes", "Yes", "Y", "true", "TRUE", "1"])
def test_truthy_market_cells_opt_the_item_in(items_tab, gsheet, cell):
    gsheet.frame = pd.DataFrame(
        [{"item": "Widget", "website": "shop.example", "market": cell}]
    )
    assert items_tab.read()[0].market is True


@pytest.mark.parametrize("cell", ["", "no", "n", "false", "0", "maybe"])
def test_other_market_cells_leave_the_item_out(items_tab, gsheet, cell):
    gsheet.frame = pd.DataFrame(
        [{"item": "Widget", "website": "shop.example", "market": cell}]
    )
    assert items_tab.read()[0].market is False


def test_a_missing_market_column_reads_as_opted_out(items_tab, gsheet):
    """An existing sheet predates the column; it must migrate, not fail."""
    gsheet.frame = pd.DataFrame([{"item": "Widget", "website": "shop.example"}])
    items = items_tab.read()
    assert len(items) == 1
    assert items[0].market is False


def test_the_market_sentinel_is_rejected_as_a_website(items_tab, gsheet):
    """Otherwise a typo silently merges an item into the market series."""
    gsheet.frame = pd.DataFrame(
        [
            {"item": "Widget", "website": "market", "market": ""},
            {"item": "Gadget", "website": "shop.example", "market": ""},
        ]
    )
    assert [i.name for i in items_tab.read()] == ["Gadget"]


def test_write_summary_places_market_columns_on_the_item_row(items_tab, gsheet):
    items = [Item(name="Widget", website="shop.example", market=True)]
    summary = pd.DataFrame(
        [
            {
                "item": "Widget",
                "website": "shop.example",
                "current": 200.0,
                "min": 190.0,
                "mean": 195.0,
                "last_checked": "2026-09-11 06:00:00",
            }
        ]
    )
    market = pd.DataFrame(
        [
            {
                "item": "Widget",
                "best_price": 100.0,
                "best_supplier": "argos.co.uk",
                "best_supplier_rating": "4.4 Trustpilot",
                "confidence": "0.9",
                "market_checked": "2026-09-11 06:00:00",
            }
        ]
    )
    items_tab.write_summary(items, summary, market)

    row = gsheet.written.iloc[0]
    assert list(gsheet.written.columns)[-1] == "market_checked"
    assert row["market"] == "yes"
    assert row["current"] == 200.0
    assert row["best_price"] == 100.0
    assert row["best_supplier"] == "argos.co.uk"
    assert row["confidence"] == "0.9"


def test_an_unswept_item_still_anchors_the_last_column(items_tab, gsheet):
    """Blank trailing cells make a sheet unreadable; 'never' prevents that."""
    items = [Item(name="Widget", website="shop.example")]
    items_tab.write_summary(items, pd.DataFrame(), pd.DataFrame())

    row = gsheet.written.iloc[0]
    assert row["market"] == ""
    assert row["best_price"] == ""
    assert row["market_checked"] == "never"
    assert row["last_checked"] == "never"
```

Match the fixture names already used in that file (`items_tab`, `gsheet`); if the existing fake exposes the written frame under a different attribute than `written`, use that name.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_items_tab.py -q`
Expected: FAIL — `TypeError: write_summary() takes 3 positional arguments but 4 were given`, and the market-column assertions failing.

- [ ] **Step 3: Implement**

In `src/price_monitor/sheets/items_tab.py`:

```python
COLUMNS = [
    "item",
    "website",
    "market",
    "current",
    "min",
    "mean",
    "last_checked",
    "best_price",
    "best_supplier",
    "best_supplier_rating",
    "confidence",
    "market_checked",
]

_SUMMARY_FIELDS = ["current", "min", "mean", "last_checked"]
_MARKET_FIELDS = ["best_price", "best_supplier", "best_supplier_rating", "confidence"]

_TRUTHY = frozenset({"yes", "y", "true", "1"})
```

Import `MARKET_KEY`:

```python
from price_monitor.models import MARKET_KEY, Item
```

In `read()`, after the existing blank-field check and before the duplicate check:

```python
            if website.lower() == MARKET_KEY:
                self.logger.warning(
                    f"Row {position}: '{MARKET_KEY}' is reserved, skipping"
                )
                continue
```

And build the item with its opt-in flag:

```python
            market = str(row.get("market") or "").strip().lower() in _TRUTHY
            items.append(Item(name=name, website=website, market=market))
```

`row.get("market")` returns `None` when the column is absent, so a pre-upgrade sheet reads as opted out. Do not add a `"market" not in frame.columns` guard — the `.get()` already handles it, and the existing guard clause only checks `item` and `website`.

Replace `write_summary`:

```python
    def write_summary(
        self, items: list[Item], summary: pd.DataFrame, market: pd.DataFrame
    ) -> None:
        """Rewrite the tab with each item's latest supplier and market figures."""
        lookup: dict[tuple[str, str], dict] = {}
        if not summary.empty:
            lookup = {
                (row["item"], row["website"]): row for row in summary.to_dict("records")
            }
        market_lookup: dict[str, dict] = {}
        if not market.empty:
            market_lookup = {row["item"]: row for row in market.to_dict("records")}

        rows = []
        for item in items:
            stats = lookup.get((item.name, item.website), {})
            swept = market_lookup.get(item.name, {})
            row = {
                "item": item.name,
                "website": item.website,
                "market": "yes" if item.market else "",
            }
            for field in _SUMMARY_FIELDS:
                row[field] = stats.get(field, "")
            for field in _MARKET_FIELDS:
                row[field] = swept.get(field, "")
            row["market_checked"] = swept.get("market_checked", "")
            # "never" (not "") anchors the row's final column, and reads
            # more clearly than a blank cell, which is ambiguous between
            # "not checked yet" and "something went wrong".
            if not row["last_checked"]:
                row["last_checked"] = "never"
            if not row["market_checked"]:
                row["market_checked"] = "never"
            rows.append(row)

        frame = pd.DataFrame(rows, columns=COLUMNS)
        # write(), not update(): this tab shrinks when the user deletes an item,
        # and without the clear a deletion would leave a duplicated last row.
        self._gsheet.write(sheet_name=self._sheet_name, df=frame)
        self.logger.info(f"Wrote summary for {len(rows)} items")
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/test_items_tab.py -q`
Expected: PASS. Existing `write_summary` tests will fail on the missing third argument — update those calls to pass `pd.DataFrame()`.

- [ ] **Step 5: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/sheets/items_tab.py tests/unit/test_items_tab.py
git commit -m "feat: add the market opt-in column and market summary columns"
```

---

## Task 9: Wire the sweep into the controller

**Files:**
- Modify: `src/price_monitor/price_monitor.py`
- Test: `tests/unit/test_price_monitor.py`

**Interfaces:**
- Consumes: `MarketSearcher.price_market`, `ItemsTab.write_summary(items, summary, market)`, `HistoryTab.market_summary()`.
- Produces: `PriceMonitor._market_one(item, last_price) -> PriceReading`; `PriceMonitor.for_test(gsheet, searcher, logger, market_searcher=None)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_price_monitor.py`, matching that file's existing fakes:

```python
def test_an_opted_in_item_is_priced_twice(monitor, gsheet, searcher, market_searcher):
    gsheet.items = [{"item": "Widget", "website": "shop.example", "market": "yes"}]
    monitor.update(force=True)

    websites = [r.website for r in searcher.readings + market_searcher.readings]
    assert "shop.example" in websites
    assert MARKET_KEY in websites
    assert len(market_searcher.calls) == 1


def test_an_opted_out_item_is_priced_once(monitor, gsheet, market_searcher):
    gsheet.items = [{"item": "Widget", "website": "shop.example", "market": ""}]
    monitor.update(force=True)
    assert market_searcher.calls == []


def test_a_failing_sweep_does_not_cost_the_supplier_price(monitor, gsheet, market_searcher):
    gsheet.items = [{"item": "Widget", "website": "shop.example", "market": "yes"}]
    market_searcher.raises = RuntimeError("sweep exploded")
    monitor.update(force=True)

    appended = gsheet.appended
    supplier = [r for r in appended if r.website == "shop.example"]
    swept = [r for r in appended if r.website == MARKET_KEY]
    assert supplier[0].status == PriceStatus.OK
    assert swept[0].status == PriceStatus.ERROR


def test_the_market_baseline_is_read_before_the_sweep_is_appended(monitor, gsheet):
    """A new low must be compared against the mean of what came before it."""
    gsheet.items = [{"item": "Widget", "website": "shop.example", "market": "yes"}]
    monitor.update(force=True)
    assert gsheet.stats_read_before_append is True
```

Extend the file's existing fake searcher with a market counterpart:

```python
class FakeMarketSearcher:
    """Returns a canned market reading, or raises if told to."""

    def __init__(self, price: float = 100.0, confidence: float = 0.9):
        self.price = price
        self.confidence = confidence
        self.calls: list[str] = []
        self.readings: list[PriceReading] = []
        self.raises: Exception | None = None

    def price_market(self, item, last_price, timestamp) -> PriceReading:
        self.calls.append(item.name)
        if self.raises is not None:
            raise self.raises
        reading = PriceReading(
            timestamp=timestamp,
            item=item.name,
            website=MARKET_KEY,
            price=self.price,
            currency="GBP",
            status=PriceStatus.OK,
            source_url="https://argos.co.uk/w",
            note="3 sources, cited, rated 4.4",
            confidence=self.confidence,
            rating="4.4 Trustpilot",
        )
        self.readings.append(reading)
        return reading


@pytest.fixture
def market_searcher():
    return FakeMarketSearcher()
```

and thread it through the existing `monitor` fixture via `PriceMonitor.for_test(..., market_searcher=market_searcher)`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_price_monitor.py -q`
Expected: FAIL — `for_test() got an unexpected keyword argument 'market_searcher'`.

- [ ] **Step 3: Implement**

In `src/price_monitor/price_monitor.py`:

Imports:

```python
from price_monitor.models import MARKET_KEY, Item, PriceReading, PriceStats, PriceStatus
from price_monitor.search.market import MarketSearcher
```

In `__init__`, pass a market searcher to `_init_parts`:

```python
        self._init_parts(
            gsheet=gsheet,
            searcher=PriceSearcher(config.llm_config, config.price_ctrl, logger),
            market_searcher=MarketSearcher(
                config.llm_config, config.price_ctrl, logger
            ),
        )
```

In `for_test`, accept and default it:

```python
    @classmethod
    def for_test(
        cls,
        gsheet: MonitoredSheetInterface,
        searcher: PriceSearcher,
        logger: logging.Logger,
        market_searcher: MarketSearcher | None = None,
    ) -> "PriceMonitor":
        """Build a controller around fakes, skipping config and network setup."""
        monitor = cls.__new__(cls)
        monitor.logger = get_child_logger(logger, cls.__name__)
        monitor.ctrl = PriceCtrl(request_delay_s=0.0)
        monitor._init_parts(
            gsheet=gsheet, searcher=searcher, market_searcher=market_searcher
        )
        monitor.root_logger = logger
        monitor.msg_client = None
        monitor.notifier = MsgNotifier(client=None, logger=monitor.logger)
        return monitor
```

In `_init_parts`, add the parameter and store it:

```python
    def _init_parts(
        self,
        gsheet: MonitoredSheetInterface,
        searcher: PriceSearcher,
        market_searcher: MarketSearcher | None = None,
    ) -> None:
        self._searcher = searcher
        self._market_searcher = market_searcher
```

In `_price_all`, add the second lookup:

```python
        for index, item in enumerate(items):
            if index and self.ctrl.request_delay_s:
                self.stop_event.wait(self.ctrl.request_delay_s)
            readings.append(
                self._price_one(item, last_prices.get((item.name, item.website)))
            )
            if item.market and self._market_searcher is not None:
                if self.ctrl.request_delay_s:
                    self.stop_event.wait(self.ctrl.request_delay_s)
                readings.append(
                    self._market_one(item, last_prices.get((item.name, MARKET_KEY)))
                )
```

Add `_market_one` beside `_price_one`:

```python
    def _market_one(self, item: Item, last_price: float | None) -> PriceReading:
        timestamp = datetime.now().replace(microsecond=0)
        try:
            return self._market_searcher.price_market(item, last_price, timestamp)
        except Exception as exc:
            # A failed sweep must never cost the item its supplier price.
            self.logger.warning(f"Market sweep for '{item.name}' failed: {exc}")
            return PriceReading(
                timestamp=timestamp,
                item=item.name,
                website=MARKET_KEY,
                price=None,
                currency=self.ctrl.currency,
                status=PriceStatus.ERROR,
                note=str(exc),
            )
```

In `_price_and_record`, pass the market summary through:

```python
            self.items_tab.write_summary(
                self.items_tab.read(),
                self.history.summarise(),
                self.history.market_summary(),
            )
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/test_price_monitor.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `pytest tests/unit -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/price_monitor.py tests/unit/test_price_monitor.py
git commit -m "feat: run a market sweep alongside the supplier lookup"
```

---

## Task 10: Gate drop notifications on confidence

**Files:**
- Modify: `src/price_monitor/app_config.py`
- Modify: `src/price_monitor/msgserver_client/msg_forwarder.py`
- Modify: `src/price_monitor/price_monitor.py`
- Modify: `config/config.example.json`
- Test: `tests/unit/test_msg_forwarder.py`, `tests/unit/test_price_monitor.py`, `tests/unit/test_app_config.py`

**Interfaces:**
- Consumes: `PriceReading.confidence`, `PriceReading.rating`.
- Produces: `PriceCtrl.market_min_confidence: float = 0.5`; `format_market_drop(reading: PriceReading, mean: float, still_falling: bool = False) -> str`.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_app_config.py`:

```python
def test_market_min_confidence_defaults_to_a_half():
    assert PriceCtrl().market_min_confidence == 0.5
```

In `tests/unit/test_msg_forwarder.py`:

```python
def test_market_drop_names_the_supplier_the_rating_and_the_confidence():
    reading = PriceReading(
        timestamp=datetime(2026, 9, 11, 6, 0, 0),
        item="Widget",
        website=MARKET_KEY,
        price=86.0,
        currency="GBP",
        status=PriceStatus.OK,
        source_url="https://argos.co.uk/w",
        confidence=0.82,
        rating="4.1 Trustpilot",
    )
    message = format_market_drop(reading, mean=100.0)
    assert "Widget" in message
    assert "argos.co.uk" in message
    assert "4.1 Trustpilot" in message
    assert "0.82" in message
    assert "14%" in message


def test_market_drop_omits_the_rating_when_the_retailer_is_unrated():
    reading = PriceReading(
        timestamp=datetime(2026, 9, 11, 6, 0, 0),
        item="Widget",
        website=MARKET_KEY,
        price=86.0,
        currency="GBP",
        status=PriceStatus.OK,
        source_url="https://tinyshop.co.uk/w",
        confidence=0.6,
        rating="",
    )
    message = format_market_drop(reading, mean=100.0)
    assert "unrated" in message
```

In `tests/unit/test_price_monitor.py`:

```python
def test_a_low_confidence_drop_is_recorded_but_not_announced(monitor, gsheet, market_searcher, notices):
    _seed_market_history(gsheet, [200.0, 200.0, 200.0])
    gsheet.items = [{"item": "Widget", "website": "shop.example", "market": "yes"}]
    market_searcher.price = 100.0
    market_searcher.confidence = 0.4

    monitor.update(force=True)

    assert any(r.website == MARKET_KEY and r.price == 100.0 for r in gsheet.appended)
    assert not any("📉" in n for n in notices)


def test_a_confident_drop_is_announced(monitor, gsheet, market_searcher, notices):
    _seed_market_history(gsheet, [200.0, 200.0, 200.0])
    gsheet.items = [{"item": "Widget", "website": "shop.example", "market": "yes"}]
    market_searcher.price = 100.0
    market_searcher.confidence = 0.9

    monitor.update(force=True)

    assert any("📉" in n and "argos.co.uk" in n for n in notices)


def test_a_supplier_drop_is_unaffected_by_the_confidence_gate(monitor, gsheet, notices):
    """Supplier readings carry no confidence, so the gate must ignore them."""
    _seed_supplier_history(gsheet, [200.0, 200.0, 200.0])
    gsheet.items = [{"item": "Widget", "website": "shop.example", "market": ""}]
    monitor.searcher_price = 100.0

    monitor.update(force=True)

    assert any("📉" in n for n in notices)
```

Write `_seed_market_history` / `_seed_supplier_history` helpers that push three prior priced rows into the fake sheet's history for `(Widget, MARKET_KEY)` and `(Widget, shop.example)` respectively — three because `PriceStats` stays silent below `_MIN_READINGS`. Reuse whatever history-seeding helper the file already has if one exists.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit -q -k "confidence or market_drop"`
Expected: FAIL — `AttributeError: 'PriceCtrl' object has no attribute 'market_min_confidence'` and `ImportError: cannot import name 'format_market_drop'`.

- [ ] **Step 3: Implement the config field**

In `src/price_monitor/app_config.py`, add to `PriceCtrl` after `drop_step`:

```python
    # A market sweep below this confidence is recorded but never announced.
    # Corroboration alone caps a single-source sweep at 0.33, so this threshold
    # is also what decides that one retailer is not a market.
    market_min_confidence: float = 0.5
```

In `config/config.example.json`, add `"market_min_confidence": 0.5` to the `price_ctrl` section.

- [ ] **Step 4: Implement the formatter**

In `src/price_monitor/msgserver_client/msg_forwarder.py`, beside `format_price_drop`:

```python
def format_market_drop(
    reading: PriceReading, mean: float, still_falling: bool = False
) -> str:
    """Render the notice sent when the best UK price drops below its mean.

    Names the retailer and its rating because, unlike a supplier drop, the
    reader has not chosen where this price comes from and needs both to judge
    whether to act on it.
    """
    change = (reading.price - mean) / mean
    trend = " still falling," if still_falling else ""
    suffix = " (suspect)" if reading.status is PriceStatus.SUSPECT else ""
    supplier = supplier_domain(reading.source_url) or "an unnamed retailer"
    rating = reading.rating or "unrated"
    return (
        f"📉 PriceMonitor — best UK price for {reading.item}: "
        f"{reading.price:.2f} {reading.currency} at {supplier} "
        f"({rating}, confidence {reading.confidence:.2f}),{trend} "
        f"below its {mean:.2f} {reading.currency} mean ({change:.0%}){suffix}"
    )
```

Add `supplier_domain` to the existing `from price_monitor.models import ...` line.

- [ ] **Step 5: Implement the gate**

In `src/price_monitor/price_monitor.py`, import `format_market_drop` alongside `format_price_drop`, and rewrite `_notify_price_drops`'s loop body:

```python
        for reading in readings:
            stats = baseline.get((reading.item, reading.website))
            if stats is None or reading.price is None:
                continue
            if not stats.new_drop_band(reading.price, self.ctrl.drop_step):
                continue
            if (
                reading.confidence is not None
                and reading.confidence < self.ctrl.market_min_confidence
            ):
                # Logged, not silent: an item that is gated every run is a
                # prompt problem worth seeing, not a quiet nothing.
                self.logger.info(
                    f"Drop for '{reading.item}' suppressed: "
                    f"confidence {reading.confidence:.2f}"
                )
                continue
            formatter = (
                format_market_drop
                if reading.website == MARKET_KEY
                else format_price_drop
            )
            self.notifier.notify(
                formatter(reading, stats.mean, still_falling=stats.last < stats.mean)
            )
```

- [ ] **Step 6: Run the tests**

Run: `pytest tests/unit -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/app_config.py src/price_monitor/msgserver_client/msg_forwarder.py src/price_monitor/price_monitor.py config/config.example.json tests/unit
git commit -m "feat: gate market drop notifications on confidence"
```

---

## Task 11: Integration test and documentation

**Files:**
- Modify: `tests/integration/test_live_search.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: everything above.
- Produces: no new code interfaces.

- [ ] **Step 1: Add the live sweep test**

Append to `tests/integration/test_live_search.py`, following its existing opt-in marker and config fixture:

```python
def test_live_market_sweep_returns_a_scored_reading(live_config, logger):
    """One real sweep. Asserts shape and plausibility, never a specific price."""
    searcher = MarketSearcher(live_config.llm_config, live_config.price_ctrl, logger)
    item = Item(name="Raspberry Pi 5 8GB", website="shop.example", market=True)

    reading = searcher.price_market(item, None, datetime.now())

    assert reading.website == MARKET_KEY
    assert reading.status in {PriceStatus.OK, PriceStatus.SUSPECT, PriceStatus.NOT_FOUND}
    if reading.price is not None:
        assert 0 < reading.price < live_config.price_ctrl.max_plausible_price
        assert 0.0 <= reading.confidence <= 1.0
        assert reading.source_url.startswith("http")
        # The report should name more than one retailer for a mainstream item;
        # if this is routinely 1, the prompt is padding-averse to a fault.
        assert "source" in reading.note
```

- [ ] **Step 2: Run it against the live API**

Run: `pytest tests/integration/test_live_search.py -q` with the integration opt-in the file already requires (env var or marker).
Expected: PASS. Record the observed confidence and note in the commit message — the first real score is the calibration datum for any later tuning.

- [ ] **Step 3: Update the README**

In the **Items tab** table, add the `market` row after `website`:

| `market` | you | `yes` to also sweep UK retailers for this item each run. Blank or anything else is no. Each sweep costs one extra billed search. |

and the four tool rows after `last_checked`: `best_price`, `best_supplier`, `best_supplier_rating`, `confidence`, `market_checked` (reads `never` for an item never swept).

In the **Prices tab** section, extend the column list to `timestamp, item, website, price, currency, source_url, note, confidence, rating, status` and note that a market sweep is recorded under the website `market`, keeping its own history, mean and drop bands separate from the tracked supplier's.

Add a new section after "The two-call lookup":

```markdown
### The market sweep

An item with `market` set to `yes` gets a second, independent lookup each run:
a UK-wide sweep that asks what the item costs across retailers generally rather
than at your tracked supplier. It is recorded as its own history row under the
website `market`, so it keeps its own mean, its own baseline and its own drop
bands.

Each sweep returns several retailers, not one. The cheapest that is in stock,
priced in the configured currency and not rated below 3.0 wins. The rest are
what make the confidence score possible.

#### What confidence means

`confidence` answers "how much does this sweep support the claim that this is
the best UK price" — not "was this number read correctly". Four factors,
multiplied:

| Factor | Full marks | Penalised when |
| --- | --- | --- |
| Corroboration | three or more retailers | fewer — a single hit is one shop found, not a market surveyed |
| Agreement | the winner is within 20% of the others' median | it sits far below them, which usually means the wrong variant, an accessory or a used unit |
| Sourcing | the winning URL is one the model actually cited | it is uncited, or absent |
| Reputation | the retailer is rated 4.0 or better | it is rated lower, down to 3.0; an unrated retailer sits neutral rather than being punished |

Because the factors multiply, one bad signal drags the whole score down. That
is deliberate for a number whose job is to catch mistakes.

A reading below `market_min_confidence` (default 0.5) is still recorded, still
updates `best_price`, and still counts toward the market mean — but it does not
trigger a drop notification, and the suppression is logged. Since corroboration
alone caps a single-source sweep at 0.33, the default threshold means an item
only one UK retailer stocks will never announce a drop. Lower
`market_min_confidence` to about 0.3 if you track such items.

The `note` column always explains the score: `3 sources, winner 8% under
median, cited, rated 4.4`.
```

Add `market_min_confidence` to the config documentation with its default and a one-line meaning.

- [ ] **Step 4: Verify the README's tables match the code**

Run: `python -c "from price_monitor.sheets.items_tab import COLUMNS; print(COLUMNS)"` and `python -c "from price_monitor.sheets.history_tab import COLUMNS; print(COLUMNS)"`
Expected: both lists match the column orders documented in the README, in order.

- [ ] **Step 5: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add tests/integration/test_live_search.py README.md
git commit -m "docs: document the market sweep and its confidence score"
```

---

## Final verification

- [ ] `pytest tests/unit -q` — all pass
- [ ] `black --check src tests && isort --check src tests && flake8 src tests` — clean
- [ ] `pytest tests/integration -q` with live credentials — passes
- [ ] Run against a real sheet with one opted-in item: `price-monitor --secrets <dir> --once --force`, then confirm the Items tab shows `best_price`, `best_supplier`, `best_supplier_rating`, `confidence` and a `market_checked` timestamp, and the Prices tab has two rows for that item
- [ ] Confirm an existing sheet with no `market` column migrates on first run: every item reads as opted out, and the tab is rewritten with the new header
