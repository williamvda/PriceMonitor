# Market Search — Design

Date: 2026-09-11

## Purpose

Today PriceMonitor prices an item at one supplier: the `website` the user typed
beside it. This adds a second, independent lookup — a UK-wide sweep that asks
what the item costs across retailers generally, reports the cheapest, and
scores how much that answer should be trusted.

The confidence score exists because a market sweep fails differently from a
site lookup. A site lookup either finds the product or does not. A sweep can
return a confident, well-formed, cheap price that belongs to an accessory, a
used unit, or the wrong variant. The score is the signal that separates "the
best UK price is £180" from "something £180-shaped turned up".

A retailer's review score feeds the same judgement. A cheap price from a
retailer nobody can buy from safely is not a usable answer, so the sweep reads
each candidate's Trustpilot (or Google) rating: an outright bad retailer is
dropped, and the range above that modulates confidence rather than switching it.

Confidence gates price-drop notifications. It does not gate recording: every
sweep is written to history regardless of score, so a suppressed drop is always
auditable after the fact.

## Scope

In scope:

- A market sweep running alongside the existing per-supplier lookup, opt-in per
  item, recorded as a second history row.
- A computed confidence score on each market reading, surfaced in its own sheet
  column.
- A retailer reputation check: a hard floor that drops the worst, and a
  confidence factor across the rest.
- Confidence gating drop notifications.

Out of scope:

- Changing how supplier lookups work. Their prompts, validation and statuses
  are untouched except where noted as a mechanical refactor.
- Recording the full candidate set. Only the winner is recorded, with the
  scoring rationale in `note`.
- Any change to timing. The sweep rides along with the existing refresh rather
  than running on its own cadence.

## Architecture

```
src/price_monitor/
  search/
    searcher.py        # unchanged job; `retrying()` lifted to module level
    market.py          # NEW  MarketSearcher: sweep prompts, candidates, ranking
    confidence.py      # NEW  score(candidates, cited_urls) -> (float, str)
    prompts.py         # + market prompt templates
    validation.py      # `normalise_price()` extracted, shared with market.py
```

Boundaries hold as before. `MarketSearcher` knows nothing about sheets.
`confidence.py` knows nothing about LLMs, clients, or I/O — it is a pure
function over a list of candidates, which is what makes it cheap to tune.

Two pieces are shared rather than copied:

- `retrying(call, label, logger)` — the exponential backoff loop currently
  private to `PriceSearcher`. Moved to module level in `searcher.py`, imported
  by `market.py`. No behaviour change.
- `normalise_price(payload, ctrl) -> (float | None, PriceStatus | None, str)` —
  the currency match, boolean/NaN rejection, VAT top-up and plausibility bounds
  from `validation.validate()`. `validate()` calls it and wraps the result in a
  `PriceReading`; the market path calls it per candidate.

The VAT rule in particular must exist in exactly one place. A market row and a
supplier row are compared against each other by the user reading the sheet; if
one path adds VAT and the other does not, that comparison is silently wrong.

## Data model

Market readings share the history tab with supplier readings, distinguished by
a sentinel website defined in `search/market.py`:

```python
MARKET_KEY = "market"
```

An item tracked at `argos.co.uk` produces two history rows per run, keyed
`(name, "argos.co.uk")` and `(name, "market")`. Every existing consumer —
`last_prices`, `price_stats`, `last_checked`, the `suspect` movement check, the
drop bands — keys off `(item, website)` and therefore works unchanged. The
market series accumulates its own mean and its own baseline, independent of the
supplier series.

This forces one guard: `ItemsTab.read()` must skip a user row whose `website`
is literally `market`, logged like a blank or duplicate row. Without it a typo
merges a user's item into the sentinel series.

```python
@dataclass(frozen=True)
class Item:
    name: str
    website: str
    market: bool = False          # NEW — opt-in, column C


@dataclass(frozen=True)
class PriceReading:
    ...
    confidence: float | None = None   # NEW — None on supplier rows
    rating: str = ""                  # NEW — e.g. "4.1 Trustpilot"; "" if unknown
```

A `Candidate` value object lives in `market.py`: supplier, price, url,
in_stock, rating, rating_count, rating_source. It is built *after*
normalisation, so its `price` is always VAT-inclusive and already known
plausible — currency and `vat_included` have served their purpose by then and
are not carried. `rating` is `float | None`; `None` means the sweep found no
rating, which is a distinct state from a bad one. It never leaves the search
layer.

## Sheet schema

### Items tab

Columns A–C are the user's; the tool owns D onward.

| Col | Column | Owner | Meaning |
| --- | --- | --- | --- |
| A | `item` | user | unchanged |
| B | `website` | user | unchanged |
| C | `market` | user | `yes`/`y`/`true`/`1`, case-insensitive, opts in. Blank or anything else is no. |
| D | `current` | tool | unchanged — supplier series |
| E | `min` | tool | unchanged — supplier series |
| F | `mean` | tool | unchanged — supplier series |
| G | `last_checked` | tool | unchanged — supplier series |
| H | `best_price` | tool | Latest market price. Blank when never swept. |
| I | `best_supplier` | tool | Domain of the winning URL. Blank when never swept. |
| J | `best_supplier_rating` | tool | The winner's review score with its source, e.g. `4.1 Trustpilot`. Blank when unknown or never swept. |
| K | `confidence` | tool | `0.00`–`1.00` for that reading. Blank when never swept. |
| L | `market_checked` | tool | Timestamp of the latest sweep of any status, or `never`. |

`market_checked` MUST stay last and MUST never be blank. This is the same trap
documented for `status` in `history_tab.py`: the Sheets API omits trailing
empty cells, and for a non-market item `best_price`, `best_supplier`,
`best_supplier_rating` and `confidence` are all blank. `market_checked` writing `never` anchors the row
width. It is not a cosmetic column, and it must not be reordered.

Its "any status" rule mirrors `last_checked`: a run of failed sweeps shows as a
moving timestamp beside a stale `best_price`, rather than looking healthy.

`write_summary` rebuilds each row from what `read()` parsed, so a `market` cell
typed as `Y` is rewritten as `yes`. This is accepted: the tool already rewrites
everything from column D on, and a normalised opt-in column is easier to read
than a mixture of spellings.

### Prices tab

`confidence` and `rating` are inserted before `status`, which stays last for
the reason documented in `history_tab.py`:

```
timestamp, item, website, price, currency, source_url, note, confidence, rating, status
```

Both are blank on supplier rows. A market row carries the winning supplier's
page in `source_url` and the scoring rationale in `note`, e.g.
`4 sources, winner 11% under median, cited, rated 4.1`.

`rating` is stored as a display string — `4.1 Trustpilot`, or `""` when the
sweep found no usable rating — not as a number plus a separate source column.
Nothing downstream does arithmetic on it: the numeric value is needed only
while scoring, which happens before the row is written. Storing it as one
string keeps the rating and its provenance together, lets `market_summary()`
pass it to the Items tab verbatim, and avoids the alternative of parsing it
back out of the free-text `note`.

`best_supplier` is derived from `source_url`'s domain rather than stored as its
own column. The model's prose name for a retailer is less trustworthy than the
URL it cited, and a column populated on one row type in ten goes stale
unnoticed.

## The market lookup

`MarketSearcher.price_market(item, last_price, timestamp) -> PriceReading`
mirrors `PriceSearcher.price()`: two calls, grounded then un-grounded, because
Gemini's grounding and structured output remain unreliable together. Nothing
raises out of it; every failure path returns a `PriceReading` carrying a status.

### Call one — grounded

Asks for a set, not a winner:

> Find current UK prices for this product from as many different UK retailers
> as you can — aim for three to six. For each: the retailer, the price shown
> now with its currency, whether it includes VAT, whether it is in stock, and
> the exact URL you read it from.
>
> For each retailer also report its Trustpilot rating out of 5 and how many
> reviews that rating is based on. If the retailer has no Trustpilot page, use
> its Google review rating instead and say which of the two you used. If you
> cannot find a rating, say unknown — do not estimate one.
>
> Only list the same product — the same model and variant. Do not list
> accessories, bundles, multipacks, refurbished or used items unless the
> product name asks for one. Do not guess a price; list only prices you
> actually saw. If you can find it at only one retailer, list only that one.

"Do not estimate one" matters as much here as "do not guess a price". A model
asked for a rating will produce a plausible 4.3 for a retailer it knows nothing
about, and an invented rating is worse than a missing one: missing is handled
as unknown and scored neutrally, while invented silently passes the floor.

The final sentence is load-bearing. A model told to find three to six will pad
to six, and padding is precisely what corrupts an agreement score. "I found
one" must be an acceptable answer for the score to mean anything.

### Call two — un-grounded

```json
{"candidates": [{"supplier": "", "price": 0, "currency": "GBP",
                 "url": "", "in_stock": true, "vat_included": true,
                 "rating": 4.1, "rating_count": 13305,
                 "rating_source": "trustpilot"}],
 "note": ""}
```

Parsed with the existing `extract_json`. `rating` and `rating_count` are
`null` when unknown; `rating_source` is `"trustpilot"`, `"google"`, or `""`.

### Ranking

1. Each raw candidate dict goes through `normalise_price`. Ones that fail —
   wrong currency, non-numeric, implausible — are dropped, not recorded.
   Survivors become `Candidate` objects carrying a VAT-inclusive price.
2. Out-of-stock candidates are dropped. An unbuyable price is not a best price.
3. The reputation floor drops any candidate rated **below 3.0 on at least 50
   reviews**. Both halves are required: a bad score from a handful of reviews
   is noise, not evidence. A candidate with no rating is never dropped here —
   plenty of legitimate specialist retailers have no Trustpilot presence, and
   dropping unknowns would bias every sweep toward the big chains and turn
   niche items into `not_found`.
4. Scoring runs over these survivors and nothing else, so `n` in the
   corroboration factor is the count of in-stock, validated, above-floor
   candidates.
5. The cheapest survivor becomes the reading's `price`; its URL becomes
   `source_url`; its domain becomes `best_supplier`; its rating becomes the
   reading's `rating` string.
6. Zero survivors → `NOT_FOUND`, like any other failed lookup.
7. The reading is then graded by the existing ladder, including `suspect` when
   the price moved more than `suspect_threshold` from the last **market**
   reading for that item.

## Confidence

```python
def score(candidates: list[Candidate], cited_urls: list[str]) -> tuple[float, str]
```

Pure. No client, no I/O, no LLM self-rating. Four factors in `[0, 1]`,
multiplied, rounded to two decimal places.

| Factor | Definition | Catches |
| --- | --- | --- |
| Corroboration | `min(n / 3, 1.0)` where `n` is the valid candidate count | A single hit is one shop found, not a market surveyed |
| Outlier | `ratio = cheapest / median(others)`, where *others* is every survivor except the cheapest (at `n = 2` that is the single other price). 1.0 at `ratio >= 0.8`, falling linearly to 0.0 at `ratio <= 0.4`. Neutral (1.0) at `n = 1`. | The characteristic failure: the cheap hit is an accessory, a used unit, or the wrong variant |
| Sourcing | 1.0 if the winner's URL is among the grounding citations; 0.6 if it is a well-formed URL that is not; 0.3 if there is no URL | A price the model recalled rather than read |
| Reputation | On the winner's rating: 1.0 at `>= 4.0`, falling linearly to 0.3 at `3.0`. Unknown — no rating, or fewer than 50 reviews — sits neutral at 0.8. Below 3.0 never reaches the scorer; the floor dropped it. | A price you would not actually act on, from a retailer you would not actually buy from |

4.0 is the point at which the reputation factor stops penalising, which is the
threshold as requested. It is a shoulder rather than a cliff on purpose: UK
majors sit right on that line — Argos 4.1, Currys 4.4, John Lewis 4.0 — so a
hard cut there would have John Lewis flickering in and out of results between
sweeps as its score moved a tenth, with nothing in the sheet to explain why the
best price jumped. A shoulder degrades the confidence smoothly instead, and the
`rating` column shows you the number that caused it.

The inverse failure is why the factor tops out at 1.0 rather than rewarding
higher scores: review-farmed dropshippers routinely sit at 4.7–4.9, so treating
a very high rating as strong evidence would favour exactly the retailers this
is meant to catch. 4.0 and 4.9 are worth the same here.

Worked examples, computed from the factors above:

| Sweep | Score |
| --- | --- |
| Four cited suppliers, prices clustered, winner rated 4.4 | 1.00 |
| Four cited suppliers, prices clustered, winner unrated | 0.80 |
| Four cited suppliers, clustered, winner rated 3.2 on 900 reviews | 0.44 |
| Four cited suppliers, winner 30% under the others' median, rated 4.4 | 0.75 |
| Four cited suppliers, winner 55% under the others' median, rated 4.4 | 0.12 |
| Two cited suppliers, clustered, winner unrated | 0.53 |
| One cited supplier, rated 4.2 | 0.33 |

### What the default threshold implies

Corroboration alone caps a single-source sweep at **0.33**, whatever else is
true of it — a perfect price from a perfectly-rated retailer still scores 0.33
if only one retailer stocks the item. With `market_min_confidence` at 0.5 that
means:

- **One retailer found → a drop is never announced.** The row is still written,
  `best_price` still updates on the Items tab, and the suppressed drop is
  logged. Only the notification is withheld.
- **Two retailers found → announced only if nothing else is wrong** (0.53 for a
  clustered, cited, unrated pair; less if any factor dips).
- **Three or more → the other three factors decide.**

This is the intended reading of "best price across all UK suppliers": one shop
is not a market. But it is a real behavioural consequence for genuinely
niche items, and the fix if it bites is to lower `market_min_confidence`
to ~0.3 rather than to change the scorer.

The outlier factor is neutral rather than punishing at `n = 1` because there is
nothing to compare against; corroboration already carries that penalty, and
applying it twice would make a single source unreachable by any evidence.

Read the score as **confidence in the claim "this is the best UK price"** — not
as confidence that the number itself was read correctly. A single-source result
is a weak market claim even when its price is exactly right. The column means
one thing rather than averaging two.

Multiplication is deliberately harsh: one bad factor drags the result down
whatever the others say. That suits a metric whose job is to catch mistakes.
It is also the number most likely to need tuning once real sweeps are visible,
which is why it lives in a pure function with a table-driven test rather than
inside the searcher.

The returned reason string travels into the history `note`, so a low score is
always explainable from the sheet alone — you can see which factor sank it
without re-running the lookup.

The three-source target, the 0.8/0.4 outlier band, the 3–6 candidate aim, the
3.0 reputation floor, the 4.0 reputation shoulder and the 50-review minimum are
module constants, not config. They are tuned by editing the scorer and
re-running its unit test, not by editing JSON. Promote them to config only if
that proves wrong.

## Control flow

`PriceMonitor._price_all` gains a second lookup for opted-in items:

```python
readings.append(self._price_one(item, last_prices.get((item.name, item.website))))
if item.market:
    readings.append(self._market_one(item, last_prices.get((item.name, MARKET_KEY))))
```

`_market_one` mirrors `_price_one`: its own try/except producing an `ERROR`
reading on `MARKET_KEY`. The two lookups are independent — a failed sweep never
costs the supplier price, and a failed supplier lookup never costs the sweep.
`request_delay_s` applies between them as it does between items.

The poll path needs no change: new items flow through `_price_and_record` and
receive both readings. `_due_for_refresh` needs no change either — it keys off
the supplier series, so the sweep rides along with the refresh.

`HistoryTab` gains `market_summary() -> pd.DataFrame` with columns
`item, best_price, best_supplier, best_supplier_rating, confidence,
market_checked`, filtered to
`website == MARKET_KEY`. `summarise()` is changed only to exclude market rows,
so the two functions partition the history cleanly rather than overlapping.
`ItemsTab.write_summary` performs two lookups per item: `(name, website)` for
D–G and `(name, MARKET_KEY)` for H–L.

`best_price` is the latest market price, not the minimum ever seen. The
question the column answers is "what is the best price available now".

## Drop detection

`_notify_price_drops` already handles market rows: they are readings with a
baseline keyed `(item, "market")`. One gate is added:

```python
if reading.confidence is not None and reading.confidence < self.ctrl.market_min_confidence:
    self.logger.info(f"Drop for '{reading.item}' suppressed: confidence {reading.confidence}")
    continue
```

Logged rather than silent. An item that is repeatedly gated is a prompt problem
that should be visible, not a quiet nothing.

The history row is still written, still counts toward the mean, and still
appears on the sheet. Only the notification is withheld.

One consequence of the market series having its own baseline: `PriceStats`
stays quiet below `_MIN_READINGS` (3), so an item newly opted in announces no
market drop until its third sweep. That is the existing rule applied to a new
series, not a new rule, and it is the right behaviour — a mean over two sweeps
describes nothing.

Market drops get their own formatter in `msg_forwarder`, because the useful
sentence differs from a supplier drop:

> Best UK price for X fell to £Y at argos.co.uk (rated 4.1 Trustpilot,
> confidence 0.82), 14% under its £Z average.

## Config

One new field in `PriceCtrl`:

```python
market_min_confidence: float = 0.5
```

`config/config.example.json` and the README gain matching entries.

## Migrating an existing sheet

Inserting `market` at column C shifts the tool columns right on a sheet that
already holds data. This self-heals, but only because of one deliberate choice:
`ItemsTab.read()` treats a missing `market` column as "no" rather than as an
error.

On an existing sheet the first refresh therefore reads no `market` column,
defaults every item to not-opted-in, and `write_summary` rewrites the tab with
the new header row and the values in their new positions. Nothing is lost — the
history tab is the source of truth and is untouched — and items are opted in
afterwards.

## Error handling

Unchanged in character. Nothing raises out of `MarketSearcher.price_market`;
every failure becomes a status:

| Failure | Status |
| --- | --- |
| Either call raises after retries | `error` |
| Empty search reply | `parse_error` |
| Unparseable JSON | `parse_error` |
| Zero candidates, or none survive validation, all out of stock, or all below the reputation floor | `not_found` |
| Winner moved more than `suspect_threshold` from the last market reading | `suspect` |

A market failure is one bad row among many; the run continues, exactly as a
failed supplier lookup does today.

## Testing

TDD throughout. The two refactors ship with regression guards.

| Suite | Covers |
| --- | --- |
| `unit/test_confidence.py` | Table-driven: candidate sets → expected score and reason. Every factor alone and in combination, plus the `n = 1` neutrality rule, the reputation shoulder at exactly 4.0 and 3.0, unknown-rating neutrality, and a sub-50-review rating treated as unknown rather than as its number |
| `unit/test_market.py` | Two-call orchestration against fake clients: happy path, zero candidates → `not_found`, all out of stock → `not_found`, unparseable JSON → `parse_error`, raising client → `error`, `suspect` against the last market reading, the reputation floor dropping a 2.1/900 candidate, an unrated candidate surviving the floor, and a 2.1/12 candidate surviving it because the review count is too low to trust |
| `unit/test_validation.py` | Extended to pin `normalise_price` behaviour **before** extraction, proving the VAT logic is unchanged by the refactor |
| `unit/test_searcher.py` | Existing tests pass untouched after `retrying` moves — the proof that the move was mechanical |
| `unit/test_items_tab.py` | `market` truthiness, absent column → all False, `website: market` rejected, round-trip write |
| `unit/test_history_tab.py` | `market_summary()`, `summarise()` excluding market rows, `confidence` round-trip, and a short-row read proving `market_checked` anchors the width |
| `unit/test_price_monitor.py` | Opted-in item → two readings, opted-out → one; drop fires above threshold and is suppressed below |
| `integration/test_live_search.py` | One real sweep against the live API, opt-in as today |

## Cost

A sweep costs one billed grounded search plus one un-grounded call — the same
as a supplier lookup. An item with `market: yes` therefore doubles its own
cost; items left opted out cost exactly what they cost today. `--force`
re-prices everything, sweeps included.

## Documentation

README gains: the `market` column in the Items tab table, the five new tool
columns, the `confidence` and `rating` columns in the Prices tab table, a short
section on what the score means and how it is computed — including why the
reputation factor is a shoulder at 4.0 rather than a cut-off, since that is the
part most likely to be mistaken for a bug — and the `market_min_confidence`
setting.
