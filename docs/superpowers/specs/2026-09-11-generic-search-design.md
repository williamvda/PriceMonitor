# Generic Search — Design

Date: 2026-09-11

Supersedes: `2026-09-11-market-search-design.md`, which added a UK-wide sweep
*alongside* per-supplier tracking. This removes the per-supplier path entirely
and makes the sweep the only lookup.

## Purpose

PriceMonitor currently prices an item at one supplier the user names. That
answers "what does my shop charge today" — not "what should I pay". This
replaces it with a single generic search across UK retailers that reports three
answers per item:

- **Best-rated** — the highest-rated retailer stocking it.
- **Cheapest** — the lowest price anyone is asking.
- **Best value** — the best-rated retailer whose premium over the cheapest
  rated one is worth paying, by a rule the user sets.

The user reads all three and decides. Only the cheapest drives statistics and
notifications, because it is the one quantity with a meaningful trend: the
best-rated retailer's price says more about that retailer than about the market.

## Scope

In scope:

- Removing the per-supplier lookup, the `website` and `market` user columns, and
  the `MARKET_KEY` sentinel.
- Three detections per sweep, each carrying price, site and rating.
- `min`/`mean` over the cheapest series, and drop notifications against it.
- A confidence score without a reputation term.

Out of scope:

- Recording every candidate a sweep found. Only the three detections are kept.
  (This is the option that would age best if the detection rules change again —
  it was rejected under YAGNI, and the trade-off is noted here deliberately.)
- Per-detection statistics. Only the cheapest has a mean.
- Any change to timing, VAT handling, retry behaviour, or the two-call shape.

## Architecture

```
src/price_monitor/
  price_monitor.py     # controller: timers, orchestration; one lookup per item
  app_config.py        # + rating_premium_pct; market_min_confidence -> min_confidence
  models.py            # Item(name), PriceReading rewritten around three detections
  search/
    market.py          # MarketSearcher: the only searcher; + detection selection
    detections.py      # NEW  pure selection of the three detections
    confidence.py      # reputation factor removed
    prompts.py         # single-site templates deleted
    searcher.py        # PriceSearcher deleted; retrying/FORMAT_PROVIDER kept
    validation.py      # validate() deleted; normalise_price kept
  sheets/
    items_tab.py       # one user column, fifteen tool columns
    history_tab.py     # one wide row per sweep
```

`detections.py` is a new pure module for the same reason `confidence.py` is
pure: the selection rules are the part most likely to need tuning once real
sweeps are visible, and tuning them should be a unit test, not a billed API
call. It has no clients, no I/O, and no knowledge of sheets.

`MarketSearcher` keeps its name although "market" is now redundant — every
lookup is a market sweep. Renaming it would churn a class the git history
already tracks for no behavioural gain.

## Data model

```python
@dataclass(frozen=True)
class Item:
    """One tracked product. The user types only its name."""

    name: str


@dataclass(frozen=True)
class Detection:
    """One retailer's offer, as one of a sweep's three answers."""

    price: float
    site: str            # bare domain, from supplier_domain(url)
    rating: str          # display string, e.g. "4.1 Trustpilot"; "" if unrated


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

`Item` loses `website`, `market` and `is_direct_url`. The identity key
everywhere becomes the item name alone; `MARKET_KEY` is deleted, since there is
no longer a second kind of row to distinguish.

A detection is `None` when the sweep could not produce it — no rated candidate
for `best_rated`, no candidates at all for any of them.

## The three detections

`search/detections.py`:

```python
def select(candidates: list[Candidate], premium_pct: float) -> tuple[
    Detection | None, Detection | None, Detection | None
]
```

Returns `(best_rated, cheapest, best_value)`. Operates only on candidates that
already survived validation and are in stock — the same list the confidence
scorer sees.

All comparison and tie-breaking happens on `Candidate`, whose `rating` is a
`float | None`. The conversion to `Detection`, whose `rating` is the display
string, happens once at the end. Comparing the display strings would sort
`"10.0 Google"` below `"4.4 Trustpilot"`.

### Best-rated

Highest `rating` among candidates that *have* one. Ties broken by lower price.
Unrated candidates are ineligible: a retailer with no rating cannot be the
best-rated one, and treating unrated as some default would invent a ranking the
evidence does not support. `None` when no candidate is rated.

### Cheapest

Lowest price. Ties broken by higher rating, then by first appearance. Present
whenever any candidate survived.

### Best value

Computed over **rated candidates only**, with the cheapest rated candidate as
the baseline `(P₀, R₀)`.

A rated candidate `(P, R)` with `R > R₀` qualifies when:

```
P - P₀  <=  (R - R₀) * premium_pct * P₀
```

That is: each rating point above the baseline earns a budget of
`premium_pct` of the baseline price, and the candidate must cost no more than
the budget its rating improvement earns. Among qualifiers, the highest rating
wins; ties go to the cheaper.

- No rated candidates → `None`.
- Exactly one rated candidate → that candidate.
- Rated candidates exist but none qualifies → the baseline itself (the cheapest
  rated one). The rule degrades to "nothing better-rated is worth the money",
  which is an answer, not a failure.

The baseline is the cheapest *rated* candidate rather than the cheapest overall
because an unrated baseline has no rating to improve on. Treating unrated as
rating zero would make every rated retailer look like a bargain worth a large
premium, which inverts the rule's purpose. A consequence worth stating: when
the overall cheapest retailer is unrated, `value_price` can exceed
`cheapest_price` by more than `premium_pct` — the user is paying for a known
retailer rather than an unknown one, which is the point.

## Confidence

`corroboration × agreement × sourcing`, multiplied and rounded to 2dp as before.
The reputation factor is **removed**.

Rating is now a visible column against every detection, so scoring it again
would count the same evidence twice. Removing it also deletes `FLOOR_RATING`,
the sub-3.0 negative-arm defect, and the shoulder-versus-cliff problem that made
Amazon UK (rated 1.4 on 33,904 Trustpilot reviews) unusable as a source — a
retailer nobody would call untrustworthy, excluded by a rule meant to catch
scammers. Trustpilot cannot distinguish "large retailer people complain about"
from "shop to avoid", so it is shown to the user rather than scored.

`MIN_RATING_COUNT` (50) survives: it still decides whether a rating is
trustworthy enough to *display* and to feed the detection rules. `MAX_RATING`
(5.0) survives for the same reason.

The confidence gate on notifications is unchanged in shape. A single-source
sweep still caps at 0.33 and so still never notifies at the default threshold.

## Sheet schema

### Items tab

Column A is the user's; the tool rewrites B onward.

| Col | Column | Owner | Meaning |
| --- | --- | --- | --- |
| A | `item` | user | The product name. The only thing the user types. |
| B | `rated_price` | tool | Best-rated detection's price. Blank if nothing rated. |
| C | `rated_site` | tool | Its retailer's domain. |
| D | `rated_rating` | tool | Its rating, e.g. `4.4 Trustpilot`. |
| E | `cheapest_price` | tool | Cheapest detection's price. |
| F | `cheapest_site` | tool | Its retailer's domain. |
| G | `cheapest_rating` | tool | Its rating; blank when that retailer is unrated. |
| H | `value_price` | tool | Best-value detection's price. |
| I | `value_site` | tool | Its retailer's domain. |
| J | `value_rating` | tool | Its rating. |
| K | `min` | tool | Lowest `cheapest_price` ever recorded. |
| L | `mean` | tool | Mean of all recorded `cheapest_price` values. |
| M | `confidence` | tool | `0.00`–`1.00` for the latest sweep. |
| N | `sources` | tool | Retailers the latest sweep found. Blank if never swept. |
| O | `last_checked` | tool | Timestamp of the latest sweep of any status, or `never`. |

`last_checked` MUST stay last and MUST never be blank. The Sheets API omits
trailing empty cells, and when every row in a read comes back short, pandas
raises inside `GoogleSheetInterface.read()` before this module sees the data —
the tab becomes permanently unreadable. Every column B–N is blank for an item
that has never been swept, so `last_checked` writing `never` is the only thing
holding the row's width. It is not cosmetic and must not be reordered.

Duplicate `item` values are skipped and logged, as duplicate `(item, website)`
pairs are today — the key is now the name alone.

### Prices tab

One row per sweep, append-only:

```
timestamp, item, rated_price, rated_site, rated_rating,
cheapest_price, cheapest_site, cheapest_rating,
value_price, value_site, value_rating,
confidence, sources, note, status
```

`status` MUST stay last, for the same truncation reason. Every other column can
be blank on a failed sweep.

## Statistics and notifications

`min`, `mean` and the drop trigger are computed over the `cheapest_price`
column alone, keyed by item name. A sweep that recorded no cheapest price
contributes nothing to them, exactly as a failed lookup does today.

Each sweep's cheapest price is compared against the mean of the sweeps recorded
*before* it, using the existing `PriceStats.new_drop_band` bands: a reading
announces only when it lands in a deeper band than the one before, so a price
sitting still stays quiet while a slow slide keeps reporting. `_MIN_READINGS`
(3) still applies, so a newly added item announces nothing until its third
sweep.

A drop below `min_confidence` is recorded, still updates every column, still
counts toward the mean — and is not announced, with the suppression logged.

The notification names the cheapest retailer, its rating and the confidence:

> 📉 PriceMonitor — Widget: 86.00 GBP at argos.co.uk (4.1 Trustpilot,
> confidence 0.82), 14% below its 100.00 GBP mean

`best_rated` and `best_value` are recorded and displayed but never trigger a
notification.

## Config

```python
    # Fraction of the baseline price the user will pay per rating point when
    # choosing the best-value detection. 0.05 = one extra rating point is
    # worth up to 5% more.
    rating_premium_pct: float = 0.05
```

`market_min_confidence` is renamed `min_confidence` — there is no longer a
non-market lookup to distinguish it from. `config/config.example.json` and the
README follow.

Everything else is unchanged: `refresh_rate_h`, `poll_rate_m`, `currency`,
`vat_rate`, `suspect_threshold`, `drop_step`, `max_plausible_price`,
`request_delay_s`.

## What is deleted

| Deleted | Why |
| --- | --- |
| `PriceSearcher` and its tests | No per-supplier lookup remains |
| `validation.validate()` | Only `PriceSearcher` called it; `normalise_price` stays |
| `_SITE_TEMPLATE`, `_PAGE_TEMPLATE`, `search_prompt`, `format_prompt`, `_FORMAT_TEMPLATE` | The single-site prompt pair |
| `Item.website`, `Item.market`, `Item.is_direct_url` | The user types only a name |
| `MARKET_KEY` | No second row kind to distinguish |
| `confidence.FLOOR_RATING`, `_reputation` | Reputation is displayed, not scored |
| The reputation floor block in `market.py._candidates` | Same |

Kept and still used: `retrying`, `FORMAT_PROVIDER`, `normalise_price`,
`extract_json`, `grounding_urls`, `supplier_domain`, `Candidate`,
`MIN_RATING_COUNT`, `MAX_RATING`, `PriceStats`, the market prompt pair.

This removes roughly 400 lines of tested code. That is correct rather than
wasteful — it is the per-supplier tool, and it is being replaced — but it is
most of what PriceMonitor originally was, and should be understood as such
before the plan is written.

## Migration

**The Items tab self-heals.** `ItemsTab.read()` looks up `item` by column name
and ignores the stale `website` and `market` columns; the first summary write
rewrites the whole tab in the new shape via `write()`'s clear-then-write. The
user's item list survives untouched and no manual step is needed.

**The Prices tab the user deletes themselves**, before the first run, and
PriceMonitor recreates it on first append. This is a documented one-line
pre-flight step, not an automated one: the tab is the user's data, it cost real
API calls to accumulate, and a tool that silently drops a history tab is a tool
that cannot be trusted with the next one. Left in place, the old rows would be
reindexed to the new columns with every price blank — harmless to the
statistics, since a blank `cheapest_price` is excluded, but confusing to read.

## Error handling

Unchanged in character. Nothing raises out of `MarketSearcher.price_market`;
every failure becomes a status on a reading with all three detections `None`:

| Failure | Status |
| --- | --- |
| Either call raises after retries | `error` |
| Empty search reply | `parse_error` |
| Unparseable JSON | `parse_error` |
| A non-list `candidates` value | `not_found` — a malformed shape is zero candidates, not an error |
| Zero candidates, none surviving validation, or all out of stock | `not_found` |
| Cheapest moved more than `suspect_threshold` from the last cheapest | `suspect` |

## Testing

| Suite | Covers |
| --- | --- |
| `unit/test_detections.py` | Each rule in isolation: best-rated ties by price; unrated ineligible for best-rated; cheapest ties by rating; the value rule with a qualifier inside budget, one outside it, exactly one rated candidate, none rated, and none qualifying (falls back to the baseline); and the unrated-cheapest case where `value_price` legitimately exceeds `cheapest_price` |
| `unit/test_confidence.py` | Rewritten without the reputation term; the three remaining factors and the 0.33 single-source cap |
| `unit/test_market.py` | The sweep producing three detections; `sources` counting survivors; every failure path yielding all-`None` detections |
| `unit/test_history_tab.py` | The wide row round-trip; `status` last; `min`/`mean` over `cheapest_price` only; a failed sweep contributing nothing |
| `unit/test_items_tab.py` | One user column; duplicate names skipped; `last_checked` anchoring a fully-blank row; the self-heal from a stale `item, website, market` sheet |
| `unit/test_price_monitor.py` | One reading per item; the drop band on the cheapest series; the confidence gate; the new notification wording |
| `integration/test_live_search.py` | Updated for the new reading shape; still opt-in, still asserts shape not prices |

## Open risk

The sweep has still never run against the live API. Prompt quality, how many
retailers a real sweep returns and what confidence scores look like in practice
remain unvalidated, and this design makes the sweep the *only* source of
prices — so that risk is now the whole tool's risk, not one feature's. The
first live run should come before this is relied on.
