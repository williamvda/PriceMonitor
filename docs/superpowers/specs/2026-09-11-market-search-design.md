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

Confidence gates price-drop notifications. It does not gate recording: every
sweep is written to history regardless of score, so a suppressed drop is always
auditable after the fact.

## Scope

In scope:

- A market sweep running alongside the existing per-supplier lookup, opt-in per
  item, recorded as a second history row.
- A computed confidence score on each market reading, surfaced in its own sheet
  column.
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
```

A `Candidate` value object lives in `market.py`: supplier, price, url,
in_stock. It is built *after* normalisation, so its `price` is always
VAT-inclusive and already known plausible — currency and `vat_included` have
served their purpose by then and are not carried. It never leaves the search
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
| J | `confidence` | tool | `0.00`–`1.00` for that reading. Blank when never swept. |
| K | `market_checked` | tool | Timestamp of the latest sweep of any status, or `never`. |

`market_checked` MUST stay last and MUST never be blank. This is the same trap
documented for `status` in `history_tab.py`: the Sheets API omits trailing
empty cells, and for a non-market item `best_price`, `best_supplier` and
`confidence` are all blank. `market_checked` writing `never` anchors the row
width. It is not a cosmetic column, and it must not be reordered.

Its "any status" rule mirrors `last_checked`: a run of failed sweeps shows as a
moving timestamp beside a stale `best_price`, rather than looking healthy.

`write_summary` rebuilds each row from what `read()` parsed, so a `market` cell
typed as `Y` is rewritten as `yes`. This is accepted: the tool already rewrites
everything from column D on, and a normalised opt-in column is easier to read
than a mixture of spellings.

### Prices tab

`confidence` is inserted before `status`, which stays last for the reason
documented in `history_tab.py`:

```
timestamp, item, website, price, currency, source_url, note, confidence, status
```

`confidence` is blank on supplier rows. A market row carries the winning
supplier's page in `source_url` and the scoring rationale in `note`, e.g.
`4 sources, winner 11% under median, cited`.

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
> Only list the same product — the same model and variant. Do not list
> accessories, bundles, multipacks, refurbished or used items unless the
> product name asks for one. Do not guess a price; list only prices you
> actually saw. If you can find it at only one retailer, list only that one.

The final sentence is load-bearing. A model told to find three to six will pad
to six, and padding is precisely what corrupts an agreement score. "I found
one" must be an acceptable answer for the score to mean anything.

### Call two — un-grounded

```json
{"candidates": [{"supplier": "", "price": 0, "currency": "GBP",
                 "url": "", "in_stock": true, "vat_included": true}],
 "note": ""}
```

Parsed with the existing `extract_json`.

### Ranking

1. Each raw candidate dict goes through `normalise_price`. Ones that fail —
   wrong currency, non-numeric, implausible — are dropped, not recorded.
   Survivors become `Candidate` objects carrying a VAT-inclusive price.
2. Out-of-stock candidates are dropped. An unbuyable price is not a best price.
3. Scoring runs over these survivors and nothing else, so `n` in the
   corroboration factor is the count of in-stock, validated candidates.
4. The cheapest survivor becomes the reading's `price`; its URL becomes
   `source_url`; its domain becomes `best_supplier`.
5. Zero survivors → `NOT_FOUND`, like any other failed lookup.
6. The reading is then graded by the existing ladder, including `suspect` when
   the price moved more than `suspect_threshold` from the last **market**
   reading for that item.

## Confidence

```python
def score(candidates: list[Candidate], cited_urls: list[str]) -> tuple[float, str]
```

Pure. No client, no I/O, no LLM self-rating. Three factors in `[0, 1]`,
multiplied, rounded to two decimal places.

| Factor | Definition | Catches |
| --- | --- | --- |
| Corroboration | `min(n / 3, 1.0)` where `n` is the valid candidate count | A single hit is one shop found, not a market surveyed |
| Outlier | `ratio = cheapest / median(others)`, where *others* is every survivor except the cheapest (at `n = 2` that is the single other price). 1.0 at `ratio >= 0.8`, falling linearly to 0.0 at `ratio <= 0.4`. Neutral (1.0) at `n = 1`. | The characteristic failure: the cheap hit is an accessory, a used unit, or the wrong variant |
| Sourcing | 1.0 if the winner's URL is among the grounding citations; 0.6 if it is a well-formed URL that is not; 0.3 if there is no URL | A price the model recalled rather than read |

Worked examples:

- Four cited suppliers, prices clustered → ~0.95
- One cited supplier → 0.33
- Four cited suppliers, winner 55% under the median of the others → ~0.5

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

The three-source target, the 0.8/0.4 outlier band, and the 3–6 candidate aim
are module constants, not config. They are tuned by editing the scorer and
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
`item, best_price, best_supplier, confidence, market_checked`, filtered to
`website == MARKET_KEY`. `summarise()` is changed only to exclude market rows,
so the two functions partition the history cleanly rather than overlapping.
`ItemsTab.write_summary` performs two lookups per item: `(name, website)` for
D–G and `(name, MARKET_KEY)` for H–K.

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

> Best UK price for X fell to £Y at argos.co.uk (confidence 0.82), 14% under
> its £Z average.

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
| Zero candidates, or none survive validation, or all out of stock | `not_found` |
| Winner moved more than `suspect_threshold` from the last market reading | `suspect` |

A market failure is one bad row among many; the run continues, exactly as a
failed supplier lookup does today.

## Testing

TDD throughout. The two refactors ship with regression guards.

| Suite | Covers |
| --- | --- |
| `unit/test_confidence.py` | Table-driven: candidate sets → expected score and reason. Every factor alone and in combination, plus the `n = 1` neutrality rule |
| `unit/test_market.py` | Two-call orchestration against fake clients: happy path, zero candidates → `not_found`, all out of stock → `not_found`, unparseable JSON → `parse_error`, raising client → `error`, `suspect` against the last market reading |
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

README gains: the `market` column in the Items tab table, the four new tool
columns, the `confidence` column in the Prices tab table, a short section on
what the score means and how it is computed, and the
`market_min_confidence` setting.
