# Generic Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace PriceMonitor's per-supplier lookup with a single generic UK-wide search that reports three answers per item — best-rated, cheapest, and best-value — with statistics and notifications driven by the cheapest.

**Architecture:** The existing market sweep becomes the only lookup. A new pure `detections.py` picks three answers from the candidate set the sweep already builds. `PriceReading` is rewritten around those three detections, both sheet tabs are reshaped, and the per-supplier code path is deleted.

**Tech Stack:** Python 3.11, pandas, pytest, llmbridge (Gemini), google-drive-api. Black + isort + flake8.

**Spec:** `docs/superpowers/specs/2026-09-11-generic-search-design.md`

## Global Constraints

- Branch: `feat/generic-search`, already checked out from `master`. Do not merge or push.
- PEP 604 unions (`X | None`), never `Optional`. No `from __future__ import annotations`.
- Annotate every function signature and dataclass field. `@dataclass(frozen=True)` for value objects.
- Module docstring on every `.py` file, kept accurate to its public surface.
- Comments only for non-obvious invariants, sign conventions, or workarounds. Narrating what the code does is a defect.
- Strict KISS/YAGNI. No new dependencies. No ABCs, no dependency injection beyond existing optional client parameters.
- `sheets/` and `msgserver_client/` must never import from `search/`. Shared value objects live in `models.py`.
- stdlib → third-party → local imports, no wildcards.
- Before every commit: `black src tests && isort src tests && flake8 src tests`, then `pytest tests/unit -q` must fully pass.
- Never run `pytest tests/integration` against live services — it makes billed API calls. `--collect-only` is fine.
- No Co-Authored-By or attribution line in any commit message.
- TDD: failing test first, run it, then implement.

### Two column invariants that must not be broken

The Sheets API omits trailing empty cells from a row. When every row in a read comes back short, pandas raises *inside* `GoogleSheetInterface.read()` before our code sees it, and the tab becomes permanently unreadable.

- **Prices tab: `status` MUST stay last.** It is always a non-empty `PriceStatus` value.
- **Items tab: `last_checked` MUST stay last and MUST never be written blank** — it writes `never` for an unswept item. Every other column on that tab is blank for an item that has never been swept, so it is the only thing holding the row's width.

### Sequencing note

Tasks 1–4 are additive or self-contained and keep the suite green. **Task 5 is the flip** — it changes `Item` and `PriceReading` and therefore touches every consumer at once. It cannot be split further without leaving the suite red at a commit, which the constraints forbid. Task 6 deletes what the flip orphaned.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `src/price_monitor/app_config.py` | MODIFY — `rating_premium_pct`; rename `market_min_confidence` → `min_confidence` |
| `src/price_monitor/models.py` | MODIFY — add `Detection`; rewrite `Item` and `PriceReading`; delete `MARKET_KEY` |
| `src/price_monitor/search/detections.py` | CREATE — pure selection of the three detections |
| `src/price_monitor/search/confidence.py` | MODIFY — remove the reputation factor and `FLOOR_RATING` |
| `src/price_monitor/search/market.py` | MODIFY — remove the floor; produce three detections |
| `src/price_monitor/search/prompts.py` | MODIFY — delete the single-site templates and functions |
| `src/price_monitor/search/searcher.py` | MODIFY — delete `PriceSearcher`; keep `retrying`, `FORMAT_PROVIDER` |
| `src/price_monitor/search/validation.py` | MODIFY — delete `validate()`; keep `normalise_price` |
| `src/price_monitor/sheets/history_tab.py` | MODIFY — wide row per sweep; stats over `cheapest_price` |
| `src/price_monitor/sheets/items_tab.py` | MODIFY — one user column, fourteen tool columns |
| `src/price_monitor/msgserver_client/msg_forwarder.py` | MODIFY — one drop formatter, built from `cheapest` |
| `src/price_monitor/price_monitor.py` | MODIFY — one lookup per item, keyed by name |
| `config/config.example.json`, `README.md` | MODIFY — new config and sheet documentation |

---

## Task 1: Config

**Files:**
- Modify: `src/price_monitor/app_config.py`
- Modify: `config/config.example.json`
- Test: `tests/unit/test_app_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `PriceCtrl.rating_premium_pct: float = 0.05` and `PriceCtrl.min_confidence: float = 0.5` (renamed from `market_min_confidence`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_app_config.py`:

```python
def test_rating_premium_defaults_to_five_percent():
    assert PriceCtrl().rating_premium_pct == 0.05


def test_min_confidence_defaults_to_a_half():
    assert PriceCtrl().min_confidence == 0.5
```

Delete the existing `test_market_min_confidence_defaults_to_a_half` — it asserts the old field name, which no longer exists. This is a rename, not a removal of coverage; the replacement above covers it.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_app_config.py -q`
Expected: FAIL — `AttributeError: 'PriceCtrl' object has no attribute 'rating_premium_pct'`.

- [ ] **Step 3: Implement**

In `PriceCtrl`, replace the `market_min_confidence` field and its comment with:

```python
    # A sweep below this confidence is recorded but never announced.
    # Corroboration alone caps a single-source sweep at 0.33, so this threshold
    # is also what decides that one retailer is not a market.
    min_confidence: float = 0.5
    # Fraction of the baseline price the user will pay per rating point when
    # choosing the best-value detection. 0.05 = one extra rating point is worth
    # up to 5% more.
    rating_premium_pct: float = 0.05
```

In `config/config.example.json`, rename `"market_min_confidence"` to `"min_confidence"` and add `"rating_premium_pct": 0.05` beside it.

- [ ] **Step 4: Update the one production reference**

`grep -rn "market_min_confidence" src tests` will find a reference in `price_monitor.py`'s `_notify_price_drops` and in `tests/unit/test_price_monitor.py`. Rename both to `min_confidence`. No behaviour changes.

- [ ] **Step 5: Run the full suite**

Run: `pytest tests/unit -q`
Expected: PASS. `grep -rn "market_min_confidence" src tests config` must return nothing.

- [ ] **Step 6: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/app_config.py config/config.example.json tests/unit
git commit -m "feat: add rating_premium_pct and rename min_confidence"
```

---

## Task 2: The Detection value object

**Files:**
- Modify: `src/price_monitor/models.py`
- Test: `tests/unit/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Detection(price: float, site: str, rating: str = "")`, frozen.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_models.py`:

```python
def test_detection_carries_price_site_and_rating():
    from price_monitor.models import Detection

    detection = Detection(price=99.99, site="argos.co.uk", rating="4.4 Trustpilot")
    assert detection.price == 99.99
    assert detection.site == "argos.co.uk"
    assert detection.rating == "4.4 Trustpilot"


def test_detection_rating_defaults_to_empty_for_an_unrated_retailer():
    from price_monitor.models import Detection

    assert Detection(price=10.0, site="shop.example").rating == ""
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'Detection'`.

- [ ] **Step 3: Implement**

In `src/price_monitor/models.py`, above `PriceReading`:

```python
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
```

- [ ] **Step 4: Run the full suite**

Run: `pytest tests/unit -q`
Expected: PASS — purely additive.

- [ ] **Step 5: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/models.py tests/unit/test_models.py
git commit -m "feat: add the Detection value object"
```

---

## Task 3: Detection selection

The heart of the feature, and the only part with no I/O. Build it in isolation with a table-driven test.

**Files:**
- Create: `src/price_monitor/search/detections.py`
- Test: `tests/unit/test_detections.py`

**Interfaces:**
- Consumes: `Detection` (Task 2), `Candidate` and its `.rated`/`.rating_label` properties, `supplier_domain` — all already on this branch.
- Produces:
  - `select(candidates: list[Candidate], premium_pct: float) -> tuple[Candidate | None, Candidate | None, Candidate | None]`, returning `(best_rated, cheapest, best_value)` as the **chosen candidates**, not detections.
  - `to_detection(candidate: Candidate | None) -> Detection | None`.

  `select` returns candidates rather than detections so its caller can recover
  each winner's VAT note **by identity**. Two candidates can legitimately share
  every field value, so matching the winner back by price and domain would pick
  the wrong one's note — a bug this codebase has already had once.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_detections.py`:

```python
"""Unit tests for selecting a sweep's three answers."""

from price_monitor.search.confidence import Candidate
from price_monitor.search.detections import select, to_detection

PREMIUM = 0.05


def _candidate(price, rating=4.0, count=900, supplier="Shop", url=None):
    return Candidate(
        supplier=supplier,
        price=price,
        url=url or f"https://{supplier.lower()}.co.uk/w",
        in_stock=True,
        rating=rating,
        rating_count=count,
        rating_source="trustpilot",
    )


def test_no_candidates_yields_no_detections():
    assert select([], PREMIUM) == (None, None, None)


def test_cheapest_is_the_lowest_price():
    cands = [_candidate(120.0, supplier="A"), _candidate(100.0, supplier="B")]
    _, cheapest, _ = select(cands, PREMIUM)
    assert cheapest.price == 100.0
    assert cheapest.url == "https://b.co.uk/w"


def test_best_rated_is_the_highest_rating_not_the_lowest_price():
    cands = [_candidate(100.0, rating=3.2, supplier="A"), _candidate(180.0, rating=4.8, supplier="B")]
    best_rated, cheapest, _ = select(cands, PREMIUM)
    assert best_rated.price == 180.0
    assert best_rated.rating_label == "4.8 Trustpilot"
    assert cheapest.price == 100.0


def test_best_rated_ties_break_toward_the_lower_price():
    cands = [_candidate(150.0, rating=4.5, supplier="A"), _candidate(120.0, rating=4.5, supplier="B")]
    best_rated, _, _ = select(cands, PREMIUM)
    assert best_rated.price == 120.0


def test_cheapest_ties_break_toward_the_higher_rating():
    cands = [_candidate(100.0, rating=3.0, supplier="A"), _candidate(100.0, rating=4.6, supplier="B")]
    _, cheapest, _ = select(cands, PREMIUM)
    assert cheapest.rating_label == "4.6 Trustpilot"


def test_an_unrated_retailer_cannot_be_the_best_rated():
    """No rating is not a rating; ranking it would invent evidence."""
    cands = [_candidate(100.0, rating=None, count=None, supplier="A"), _candidate(150.0, rating=4.0, supplier="B")]
    best_rated, cheapest, _ = select(cands, PREMIUM)
    assert best_rated.price == 150.0
    assert cheapest.price == 100.0
    assert cheapest.rating_label == ""


def test_nothing_rated_leaves_best_rated_and_best_value_empty():
    cands = [_candidate(100.0, rating=None, count=None), _candidate(120.0, rating=None, count=None)]
    best_rated, cheapest, best_value = select(cands, PREMIUM)
    assert best_rated is None
    assert best_value is None
    assert cheapest.price == 100.0


def test_a_thinly_reviewed_rating_does_not_count_as_rated():
    """4.9 from 12 reviews is noise — ineligible, exactly as unrated is."""
    cands = [_candidate(100.0, rating=4.9, count=12, supplier="A")]
    best_rated, cheapest, best_value = select(cands, PREMIUM)
    assert best_rated is None
    assert best_value is None
    assert cheapest.price == 100.0


def test_best_value_takes_an_upgrade_inside_the_budget():
    # Baseline 100 at 3.0. B is +1.0 rating, earning 1.0 * 5% * 100 = 5.00.
    # B costs +4.00, inside budget.
    cands = [_candidate(100.0, rating=3.0, supplier="A"), _candidate(104.0, rating=4.0, supplier="B")]
    _, _, best_value = select(cands, PREMIUM)
    assert best_value.price == 104.0


def test_best_value_refuses_an_upgrade_outside_the_budget():
    # C is +1.5 rating, earning 1.5 * 5% * 100 = 7.50, but costs +18.00.
    cands = [_candidate(100.0, rating=3.0, supplier="A"), _candidate(118.0, rating=4.5, supplier="C")]
    _, _, best_value = select(cands, PREMIUM)
    assert best_value.price == 100.0


def test_best_value_falls_back_to_the_baseline_when_nothing_qualifies():
    """'Nothing better-rated is worth the money' is an answer, not a failure."""
    cands = [_candidate(100.0, rating=3.0, supplier="A"), _candidate(500.0, rating=5.0, supplier="B")]
    _, _, best_value = select(cands, PREMIUM)
    assert best_value.price == 100.0


def test_best_value_never_downgrades_to_a_worse_rating():
    cands = [_candidate(100.0, rating=3.0, supplier="A"), _candidate(99.0, rating=2.0, supplier="B")]
    _, cheapest, best_value = select(cands, PREMIUM)
    assert cheapest.price == 99.0
    assert best_value.price == 100.0


def test_a_single_rated_candidate_is_its_own_best_value():
    cands = [_candidate(100.0, rating=4.2, supplier="A")]
    _, _, best_value = select(cands, PREMIUM)
    assert best_value.price == 100.0


def test_best_value_baseline_ignores_an_unrated_cheaper_retailer():
    """The baseline is the cheapest *rated* candidate.

    An unrated baseline has no rating to improve on, and scoring it as zero
    would make every rated shop look like a bargain worth a large premium.
    So value_price can legitimately exceed cheapest_price here.
    """
    cands = [
        _candidate(50.0, rating=None, count=None, supplier="Unknown"),
        _candidate(100.0, rating=3.0, supplier="A"),
        _candidate(104.0, rating=4.0, supplier="B"),
    ]
    _, cheapest, best_value = select(cands, PREMIUM)
    assert cheapest.price == 50.0
    assert best_value.price == 104.0


def test_to_detection_converts_a_winner_for_the_sheet():
    detection = to_detection(_candidate(100.0, rating=4.4, supplier="Argos"))
    assert detection.price == 100.0
    assert detection.site == "argos.co.uk"
    assert detection.rating == "4.4 Trustpilot"


def test_to_detection_passes_none_through():
    assert to_detection(None) is None


def test_best_value_ties_break_toward_the_cheaper():
    cands = [
        _candidate(100.0, rating=3.0, supplier="A"),
        _candidate(103.0, rating=4.0, supplier="B"),
        _candidate(104.0, rating=4.0, supplier="C"),
    ]
    _, _, best_value = select(cands, PREMIUM)
    assert best_value.price == 103.0
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_detections.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'price_monitor.search.detections'`.

- [ ] **Step 3: Implement**

Create `src/price_monitor/search/detections.py`:

```python
"""Selecting a sweep's three answers from its candidates.

Best-rated, cheapest, and the best-value compromise between them. Pure: no
clients, no I/O, no sheets. These rules are the part most likely to need tuning
once real sweeps are visible, and tuning them should cost a unit test rather
than a billed API call.
"""

from price_monitor.models import Detection, supplier_domain
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
    cheapest = min(candidates, key=lambda c: (c.price, -(c.rating or 0.0)))
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
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/test_detections.py -v`
Expected: PASS, all cases.

- [ ] **Step 5: Run the full suite**

Run: `pytest tests/unit -q`
Expected: PASS — purely additive.

- [ ] **Step 6: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/search/detections.py tests/unit/test_detections.py
git commit -m "feat: select best-rated, cheapest and best-value from a sweep"
```

---

## Task 4: Confidence without the reputation factor

**Files:**
- Modify: `src/price_monitor/search/confidence.py`
- Modify: `src/price_monitor/search/market.py`
- Test: `tests/unit/test_confidence.py`, `tests/unit/test_market.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `score(candidates, cited_urls) -> tuple[float, str]` computed from three factors. `FLOOR_RATING` no longer exists. `MIN_RATING_COUNT` and `MAX_RATING` are unchanged.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_confidence.py`, replace the whole expected-score table and the reputation tests. Rating no longer affects the score at all, so:

```python
@pytest.mark.parametrize(
    "candidates, expected",
    [
        (_clustered(4), 1.00),
        (_clustered(4, rating=None, rating_count=None), 1.00),
        (_clustered(4, rating=1.4, rating_count=33904), 1.00),
        (_clustered(2), 0.67),
        (_clustered(1), 0.33),
    ],
)
def test_scores_depend_on_the_sweep_not_the_retailer(candidates, expected):
    assert score(candidates, [CITED])[0] == expected


def test_a_poorly_rated_retailer_no_longer_lowers_confidence():
    """Rating is shown against each detection; scoring it too would double-count.

    Amazon UK sits at 1.4 on tens of thousands of Trustpilot reviews — a
    retailer nobody would call untrustworthy. The rating cannot separate
    'large retailer people complain about' from 'shop to avoid', so it is
    displayed rather than scored.
    """
    good = score(_clustered(4, rating=4.8), [CITED])[0]
    bad = score(_clustered(4, rating=1.4, rating_count=33904), [CITED])[0]
    assert good == bad == 1.00


def test_score_never_leaves_the_unit_interval():
    for rating in (0.0, 1.0, 2.0, 3.0, 4.0, 5.0, None):
        value = score(_clustered(4, rating=rating, rating_count=900), [CITED])[0]
        assert 0.0 <= value <= 1.0


def test_the_reason_no_longer_mentions_a_rating():
    _, reason = score(_clustered(4), [CITED])
    assert "rated" not in reason
    assert "4 sources" in reason
```

Delete `test_the_reputation_shoulder_is_flat_at_and_above_four`, `test_the_reputation_factor_bottoms_out_at_three`, and `test_a_rating_on_too_few_reviews_reads_as_unknown` — all three assert a factor that no longer exists. `rating_label`'s own test stays; the property still drives the sheet.

In `tests/unit/test_market.py`, replace `test_a_badly_rated_retailer_is_dropped_by_the_floor` with:

```python
def test_a_badly_rated_retailer_is_no_longer_dropped(logger):
    """The floor is gone: a 1.4-rated retailer competes on price like anyone.

    Its rating travels to the sheet so the reader can judge it.
    """
    searcher = _searcher(
        logger,
        [
            _candidate(price=50.0, rating=1.4, rating_count=33904, url="https://a.co.uk/w"),
            _candidate(price=100.0, url="https://b.co.uk/w"),
        ],
    )
    reading = searcher.price_market(ITEM, None, NOW)
    assert reading.price == 50.0
    assert reading.sources == 2
```

Note this test asserts on the *current* `PriceReading` shape and will be rewritten again in Task 5. That is expected — keeping the suite green at every commit is worth one test being touched twice.

Also update `test_the_source_count_excludes_candidates_that_were_dropped`: remove its `rating=2.1, rating_count=900` candidate (no longer dropped) and adjust its expectation to `sources == 1` with the remaining out-of-stock, wrong-currency and negative-price candidates.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_confidence.py tests/unit/test_market.py -q`
Expected: FAIL — the parametrized scores return reputation-weighted values (e.g. 0.80 where 1.00 is now expected).

- [ ] **Step 3: Implement the confidence change**

In `src/price_monitor/search/confidence.py`:

- Delete `FLOOR_RATING`, `_RATING_FULL`, `_RATING_FLOOR_SCORE`, `_UNKNOWN_RATING_SCORE` and the whole `_reputation` function.
- In `score()`, drop the `reputation` term from the product and from the reason string:

```python
    value = round(corroboration * outlier * sourcing, 2)
    plural = "" if len(candidates) == 1 else "s"
    reason = (
        f"{len(candidates)} source{plural}, {outlier_reason}, {sourcing_reason}"
    )
```

- Update the module docstring: it currently says four factors; it is three.
- Keep `MIN_RATING_COUNT`, `Candidate`, `.rated` and `.rating_label` exactly as they are — the detection rules and the sheet both depend on them.

- [ ] **Step 4: Implement the market change**

In `src/price_monitor/search/market.py`:

- Remove `FLOOR_RATING` from the `confidence` import.
- Delete the floor block in `_candidates` — the `if candidate.rated and candidate.rating < FLOOR_RATING:` branch and its logging line and comment.
- Keep the `MAX_RATING` range guard: a rating outside `[0, 5]` is still treated as unknown, because it is not a trustworthy five-point score.

- [ ] **Step 5: Run the tests**

Run: `pytest tests/unit -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src/price_monitor/search/confidence.py src/price_monitor/search/market.py tests/unit
git commit -m "feat: display retailer ratings instead of scoring them"
```

---

## Task 5: The flip

This is the large one. It changes `Item` and `PriceReading`, so every consumer moves together. It cannot be split without leaving the suite red at a commit.

**Files:**
- Modify: `src/price_monitor/models.py`, `src/price_monitor/search/market.py`, `src/price_monitor/sheets/history_tab.py`, `src/price_monitor/sheets/items_tab.py`, `src/price_monitor/msgserver_client/msg_forwarder.py`, `src/price_monitor/price_monitor.py`
- Test: the matching files under `tests/unit/`

**Interfaces:**
- Consumes: `Detection` (Task 2), `select()` and `to_detection()` (Task 3), `PriceCtrl.rating_premium_pct` (Task 1).
- Produces:
  - `Item(name: str)` — no other fields.
  - `PriceReading(timestamp, item, currency, status, best_rated=None, cheapest=None, best_value=None, confidence=None, sources=None, note="")`.
  - `HistoryTab.price_stats() -> dict[str, PriceStats]` and `.last_checked() -> dict[str, datetime]`, both keyed by item name.
  - `HistoryTab.summarise() -> pd.DataFrame` with `SUMMARY_COLUMNS`.
  - `ItemsTab.write_summary(items: list[Item], summary: pd.DataFrame) -> None` — back to two arguments.
  - `format_price_drop(reading: PriceReading, mean: float, still_falling: bool = False) -> str`.

- [ ] **Step 1: Rewrite the value objects**

In `src/price_monitor/models.py`:

```python
@dataclass(frozen=True)
class Item:
    """One tracked product. The user types only its name."""

    name: str


@dataclass(frozen=True)
class PriceReading:
    """One sweep's result for one item."""

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
```

Delete `MARKET_KEY` and `Item.is_direct_url`. Keep `supplier_domain` — `detections.py` uses it. Update the module docstring.

- [ ] **Step 2: Rewrite the searcher's output**

In `src/price_monitor/search/market.py`, `_rank` becomes:

```python
        candidates = [candidate for candidate, _ in pairs]
        rated_pick, cheapest_pick, value_pick = select(
            candidates, self._ctrl.rating_premium_pct
        )
        # `is`, not `==`: two candidates can share every field value, and only
        # the object actually chosen carries the right VAT note.
        winner_vat_note = next(n for c, n in pairs if c is cheapest_pick)
        value, reason = score(candidates, urls)

        status = PriceStatus.OK
        note = reason
        if winner_vat_note:
            note = f"{note}; {winner_vat_note}"
        if last_price is not None and last_price > 0:
            move = abs(cheapest_pick.price - last_price) / last_price
            if move > self._ctrl.suspect_threshold:
                status = PriceStatus.SUSPECT
                note = f"{note}; moved {move:.0%} from {last_price}"

        return PriceReading(
            timestamp=timestamp,
            item=item.name,
            currency=self._ctrl.currency,
            status=status,
            best_rated=to_detection(rated_pick),
            cheapest=to_detection(cheapest_pick),
            best_value=to_detection(value_pick),
            confidence=value,
            sources=len(candidates),
            note=note,
        )
```

`_failed` drops its `website` argument and leaves all three detections `None`. Add `from price_monitor.search.detections import select, to_detection`.

- [ ] **Step 3: Rewrite the history tab**

`COLUMNS` becomes, with `status` still last:

```python
COLUMNS = [
    "timestamp",
    "item",
    "rated_price",
    "rated_site",
    "rated_rating",
    "cheapest_price",
    "cheapest_site",
    "cheapest_rating",
    "value_price",
    "value_site",
    "value_rating",
    "confidence",
    "sources",
    "note",
    "status",
]

SUMMARY_COLUMNS = [
    "item",
    "rated_price",
    "rated_site",
    "rated_rating",
    "cheapest_price",
    "cheapest_site",
    "cheapest_rating",
    "value_price",
    "value_site",
    "value_rating",
    "min",
    "mean",
    "confidence",
    "sources",
    "last_checked",
]
```

Delete `MARKET_SUMMARY_COLUMNS`, `market_summary()`, `last_prices()` in its two-key form and `known_items()` in its two-key form — rewrite the latter two keyed by item name. `_to_row` gains a helper:

```python
def _detection_cells(prefix: str, detection: Detection | None) -> dict[str, str]:
    if detection is None:
        return {f"{prefix}_price": "", f"{prefix}_site": "", f"{prefix}_rating": ""}
    return {
        f"{prefix}_price": f"{detection.price:.2f}",
        f"{prefix}_site": detection.site,
        f"{prefix}_rating": detection.rating,
    }
```

`_typed()` coerces `cheapest_price` instead of `price`. `price_stats()`, `last_prices()`, `last_checked()`, `known_items()` and `summarise()` all group by `"item"` alone and read `cheapest_price`. `summarise()` reports the latest row's nine detection cells plus `min`/`mean` over `cheapest_price`, plus `confidence`, `sources` and `last_checked`.

- [ ] **Step 4: Rewrite the items tab**

```python
COLUMNS = [
    "item",
    "rated_price",
    "rated_site",
    "rated_rating",
    "cheapest_price",
    "cheapest_site",
    "cheapest_rating",
    "value_price",
    "value_site",
    "value_rating",
    "min",
    "mean",
    "confidence",
    "sources",
    "last_checked",
]
_SUMMARY_FIELDS = COLUMNS[1:-1]
```

`read()` requires only the `item` column, keys duplicates on the name alone, and drops the `website`/`market`/sentinel logic entirely. `write_summary(items, summary)` returns to two arguments, looks up by name, and keeps the `last_checked` → `never` substitution with its existing comment — that comment is now the *only* thing documenting the trailing-cell invariant on this tab, so strengthen it to say every other column is blank for an unswept item.

- [ ] **Step 5: Rewrite the notification**

In `msg_forwarder.py`, delete `format_market_drop` and rewrite `format_price_drop`:

```python
def format_price_drop(
    reading: PriceReading, mean: float, still_falling: bool = False
) -> str:
    """Render the notice sent when an item's cheapest price drops below its mean.

    Names the retailer and its rating: the reader did not choose where this
    price comes from and needs both to judge whether to act on it.
    """
    cheapest = reading.cheapest
    change = (cheapest.price - mean) / mean
    trend = " still falling," if still_falling else ""
    suffix = " (suspect)" if reading.status is PriceStatus.SUSPECT else ""
    rating = cheapest.rating or "unrated"
    return (
        f"📉 PriceMonitor — {reading.item}: {cheapest.price:.2f} "
        f"{reading.currency} at {cheapest.site} ({rating}, confidence "
        f"{reading.confidence:.2f}),{trend} below its {mean:.2f} "
        f"{reading.currency} mean ({change:.0%}){suffix}"
    )
```

`format_check_complete` counts readings whose `cheapest` is not `None` rather than whose `price` is not `None`.

- [ ] **Step 6: Rewrite the controller**

In `price_monitor.py`: delete the `PriceSearcher` import and field, the `_price_one` method, and the `swept` deduplication (one sweep per item is now structural). `_price_all` becomes one `_sweep_one(item, last_price)` call per item. Every dictionary key becomes `item.name`. `write_summary` takes two arguments again. `_notify_price_drops` reads `reading.cheapest` and skips readings whose `cheapest` is `None`, gating on `self.ctrl.min_confidence`.

- [ ] **Step 7: Rewrite the tests**

Every test file that constructs an `Item` or `PriceReading` needs updating. Work through them in this order, running each file as you go: `test_models.py`, `test_market.py`, `test_history_tab.py`, `test_items_tab.py`, `test_msg_forwarder.py`, `test_price_monitor.py`.

Preserve what each test *asserts* wherever the behaviour still exists. Specifically keep, in their new shape:

- the trailing-column tests on both tabs (`status` last; `last_checked` last and never blank)
- the `suspect` movement check against the last cheapest price
- the confidence gate suppressing a drop, and a drop firing above the threshold
- the drop-band behaviour (no repeat notification within a band)
- every `MarketSearcher` failure path yielding a reading with all three detections `None`
- `sources` counting survivors

Delete only tests whose subject no longer exists: the `market` opt-in column, the `website` sentinel guard, the two-key summary lookups, `market_summary()`, and `format_market_drop`.

- [ ] **Step 8: Add the migration test**

In `tests/unit/test_items_tab.py`:

```python
def test_a_stale_three_column_sheet_still_reads(logger):
    """An existing sheet has item/website/market; only `item` matters now."""
    frame = pd.DataFrame(
        [
            {"item": "Widget", "website": "shop.example", "market": "yes"},
            {"item": "Gadget", "website": "other.example", "market": ""},
        ]
    )
    items = ItemsTab(FakeSheet(frame), "Items", logger).read()
    assert [i.name for i in items] == ["Widget", "Gadget"]
```

- [ ] **Step 9: Run the full suite**

Run: `pytest tests/unit -q`
Expected: PASS. If `tests/integration/test_live_search.py` fails to import, leave it — Task 7 rewrites it; `pytest tests/unit` must not depend on it.

- [ ] **Step 10: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src tests
git commit -m "feat: report best-rated, cheapest and best-value per item

Replaces per-supplier tracking with a single generic search. The identity
key becomes the item name, both tabs carry the three detections, and
statistics and notifications run on the cheapest series alone."
```

---

## Task 6: Delete the per-supplier path

Nothing references these after Task 5. Deleting them is what makes the flip complete rather than half-done.

**Files:**
- Modify: `src/price_monitor/search/searcher.py`, `src/price_monitor/search/prompts.py`, `src/price_monitor/search/validation.py`
- Delete: `tests/unit/test_searcher.py` and the `validate()` tests in `tests/unit/test_validation.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `searcher.py` exporting only `retrying`, `FORMAT_PROVIDER`, `_is_retryable` and the retry constants.

- [ ] **Step 1: Confirm nothing references them**

Run: `grep -rn "PriceSearcher\|search_prompt\|format_prompt\|\bvalidate(" src tests`
Expected: hits only in the files about to be deleted. If `src/` outside those files still references any, stop and report — Task 5 is incomplete.

- [ ] **Step 2: Delete**

- `searcher.py`: delete the `PriceSearcher` class and any import left unused (`LLMClient`, `Item`, `PriceReading`, `PriceStatus`, `grounding_urls`, `extract_json`, `search_prompt`, `format_prompt`, `validate`, `datetime`, `get_child_logger`, `LLMConfig`, `PriceCtrl`). Keep `retrying`, `FORMAT_PROVIDER`, `_is_retryable`, `_RETRY_ATTEMPTS`, `_RETRY_BASE_DELAY_S`, `_RETRYABLE_STATUS`. Rewrite the module docstring — it describes a two-call lookup that now lives in `market.py`.
- `prompts.py`: delete `search_prompt`, `format_prompt`, `_SITE_TEMPLATE`, `_PAGE_TEMPLATE`, `_FORMAT_TEMPLATE`. Update the module docstring.
- `validation.py`: delete `validate()`. Keep `normalise_price`. Update the module docstring. The `Item`, `PriceReading` and `datetime` imports become unused — remove them.
- `tests/unit/test_searcher.py`: delete the file. Its retry coverage moves — see Step 3.
- `tests/unit/test_validation.py`: delete every `validate()` test; keep the `normalise_price` tests.
- `tests/unit/test_prompts.py`: delete the single-site prompt tests; keep the market prompt tests.

- [ ] **Step 3: Preserve the retry coverage**

`test_searcher.py` was the only suite covering `retrying`'s backoff. Do not lose it — create `tests/unit/test_retrying.py` with the retry cases from the deleted file, calling `retrying()` directly:

```python
"""Unit tests for the shared retry loop."""

import logging

import pytest
from llmbridge.exceptions import LLMConnectionError, LLMResponseError

from price_monitor.search.searcher import _RETRY_ATTEMPTS, retrying


@pytest.fixture
def logger():
    return logging.getLogger("test")


def test_a_successful_call_is_not_retried(logger):
    calls = []
    retrying(lambda: calls.append(1) or "ok", "test", logger)
    assert len(calls) == 1


def test_a_connection_error_is_retried_to_exhaustion(logger, monkeypatch):
    monkeypatch.setattr("price_monitor.search.searcher.time.sleep", lambda _: None)
    calls = []

    def failing():
        calls.append(1)
        raise LLMConnectionError("down")

    with pytest.raises(LLMConnectionError):
        retrying(failing, "test", logger)
    assert len(calls) == _RETRY_ATTEMPTS


def test_a_rate_limit_is_not_retried(logger):
    """429 signals a standing project quota, not a burst that clears."""
    calls = []

    def failing():
        calls.append(1)
        raise LLMResponseError("quota", status_code=429)

    with pytest.raises(LLMResponseError):
        retrying(failing, "test", logger)
    assert len(calls) == 1
```

Check the deleted file first for any further retry cases and carry those across too.

- [ ] **Step 4: Run the full suite**

Run: `pytest tests/unit -q`
Expected: PASS, with a smaller test count. `flake8` must report no unused imports.

- [ ] **Step 5: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add src tests
git commit -m "refactor: delete the per-supplier lookup path"
```

---

## Task 7: Integration test and documentation

**Files:**
- Modify: `tests/integration/test_live_search.py`, `README.md`

**Interfaces:**
- Consumes: everything above.
- Produces: no new code interfaces.

- [ ] **Step 1: Update the integration test**

The file's supplier-path tests (`PriceSearcher`, `search_prompt`, the grounded/un-grounded fallback checks) no longer compile — delete them. Keep and update the market sweep test for the new reading shape:

```python
def test_live_sweep_returns_three_detections(config, logger):
    """One real sweep. Asserts shape and plausibility, never a specific price."""
    searcher = MarketSearcher(config.llm_config, config.price_ctrl, logger)
    item = Item(name="Raspberry Pi 5 8GB")

    reading = searcher.price_market(item, None, datetime.now())

    assert reading.status in {PriceStatus.OK, PriceStatus.SUSPECT, PriceStatus.NOT_FOUND}
    if reading.cheapest is None:
        return
    assert 0 < reading.cheapest.price < config.price_ctrl.max_plausible_price
    assert reading.cheapest.site
    assert 0.0 <= reading.confidence <= 1.0
    if reading.best_value is not None and reading.best_rated is not None:
        assert reading.best_value.price >= reading.cheapest.price or (
            reading.cheapest.rating == ""
        )
    sources = int(reading.note.split()[0])
    assert sources >= 2, f"sweep found only {sources} retailer(s): {reading.note}"
```

**Do not run this against the live API.** It makes billed Gemini search-grounding calls against the user's credentials. Verify only that it collects and skips:

```
pytest tests/integration --collect-only -q
pytest tests/integration -q -m integration
```
Expected: collection succeeds with no import or fixture errors, and every test reports skipped (no `PRICE_MONITOR_SECRETS`). Say in your report that the live run was not performed.

- [ ] **Step 2: Rewrite the README**

Read `README.md` in full first; it explains *why* things are as they are, and the new material must match that register rather than reading as a bolt-on.

Rewrite:
- **The overview and "How it works"** — there is no per-site scraping story any more; every item gets one generic UK search.
- **The Items tab table** — one user column (`item`) and fourteen tool columns. Delete `website`, `market`, `best_supplier*`.
- **The Prices tab** — the new fifteen-column layout, one row per sweep.
- **A new section explaining the three detections**, including the best-value rule with a worked example, and the fact that `value_price` can exceed `cheapest_price` when the cheapest retailer is unrated — that is the single most likely thing to be reported as a bug.
- **The confidence section** — three factors now, not four. Say plainly that retailer ratings are *shown, not scored*, and why: Trustpilot cannot tell "large retailer people complain about" from "shop to avoid", and Amazon UK at 1.4 from tens of thousands of reviews is the proof.
- **Config** — `rating_premium_pct` and the renamed `min_confidence`.
- **A migration note** — delete the Prices tab before the first run; the Items tab migrates itself.

Keep the existing single-source-caps-at-0.33 warning; it is unchanged and still the most surprising behaviour.

- [ ] **Step 3: Verify the docs against the code**

```
python -c "from price_monitor.sheets.items_tab import COLUMNS; print(COLUMNS)"
python -c "from price_monitor.sheets.history_tab import COLUMNS; print(COLUMNS)"
```
Both must match the README's tables exactly, in order. Fix the README if they disagree — the code is right.

- [ ] **Step 4: Commit**

```bash
black src tests && isort src tests && flake8 src tests
git add tests/integration README.md
git commit -m "docs: document the generic search and its three detections"
```

---

## Final verification

- [ ] `pytest tests/unit -q` — all pass
- [ ] `black --check src tests && isort --check src tests && flake8 src tests` — clean
- [ ] `pytest tests/integration --collect-only -q` — collects, no errors
- [ ] `grep -rn "MARKET_KEY\|PriceSearcher\|market_min_confidence\|best_supplier" src tests` — returns nothing
- [ ] `python -c "from price_monitor.sheets.items_tab import COLUMNS; print(COLUMNS[-1])"` prints `last_checked`
- [ ] `python -c "from price_monitor.sheets.history_tab import COLUMNS; print(COLUMNS[-1])"` prints `status`
- [ ] Live run against a real sheet remains outstanding and must be surfaced to the user
