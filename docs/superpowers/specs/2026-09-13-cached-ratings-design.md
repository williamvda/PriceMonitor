# Cached Retailer Ratings — Design

Date: 2026-09-13

## Purpose

The sweep prompt asks, for every retailer it finds, "report its Trustpilot
rating out of 5 and how many reviews that rating is based on." Gemini obeys by
reading Trustpilot during the same grounded call that is supposed to be finding
retailers. The live logs show the cost directly:

```
Epson    : pricespy, trustpilot, printerland, printerland, trustpilot, cartridgepeople
GoodHome : diy, trade-point, diy, trustpilot, trade-point, trade-point
```

Two of Epson's six grounding passages went to `trustpilot.com`. Those passages
are not finding shops, and breadth is the feature's weakest point.

Ratings also move far more slowly than prices — months against days — so the
sweep pays a monthly-changing fact's cost at a daily rate. Worse, it pays it
*inconsistently*: the same retailer has come back as `4.0 Trustpilot`, then
`4.2 Google`, then `4.2 Trustpilot` across three runs of the same query,
because the model re-decides its source every time.

This moves ratings out of the sweep into a cache with its own slow refresh. The
sweep spends its whole effort on prices and retailers, and a retailer's rating
stops changing for reasons that have nothing to do with the retailer.

## Scope

In scope:

- A `Ratings` tab holding one row per retailer.
- A batched lookup that fills it, folded into the existing refresh.
- Removing the rating request from the sweep prompt and its JSON schema.
- Building `Candidate` ratings from the cache rather than from the model's reply.

Out of scope:

- Any change to how ratings are *used*. `.rated`, `rating_label` and
  `detections.select` are untouched — only the source of the number changes.
- A separate timer. The refresh rides on `update()`; see "Refresh policy".
- Rating providers beyond Trustpilot and Google, which is what the sweep
  already asks for.

## The gap that shapes the design

Only the three winning detections reach the Prices tab. A retailer that
appeared in a sweep and lost is recorded nowhere — so nothing would ever know
to fetch its rating, so it could never become best-rated. The cache would
slowly fill with winners and permanently exclude everyone else.

So **the sweep registers what it saw**. After ranking, every surviving
candidate's site that is absent from the `Ratings` tab is appended as a
placeholder row: a site name, no rating, no date. The refresh then rates
whatever is unrated or stale.

Without this the feature is circular and useless. It is the load-bearing part
of the design, not an optimisation.

## Architecture

```
src/price_monitor/
  sheets/
    ratings_tab.py     # NEW  read, register, write the Ratings tab
  search/
    ratings.py         # NEW  RatingsLookup: batched grounded lookup + prompt
    market.py          # ratings passed in, as `blocked` already is
    prompts.py         # the rating request leaves the sweep templates
  price_monitor.py     # refresh folded into update(); registration after a sweep
```

`RatingsLookup` knows nothing about sheets; `RatingsTab` knows nothing about
LLMs. The controller owns the sequencing, as it does for every other pairing
here.

## Data flow

```
update()
  1. refresh ratings   — sites missing or older than rating_rate_d,
                         one grounded call, at most one batch per update
  2. sweep each item   — ratings passed in like `blocked`
  3. register          — unseen candidate sites appended as placeholders
```

Refresh runs *before* the sweeps so the current run benefits from whatever the
last run registered. A site first seen today is therefore rated tomorrow, which
is the one-cycle delay accepted in design.

## The Ratings tab

| Column | Owner | Meaning |
| --- | --- | --- |
| `site` | tool | Bare domain, as `supplier_domain` renders it. |
| `rating` | tool | Score out of 5. Blank when none was found. |
| `reviews` | tool | Review count. Blank when none was found. |
| `source` | tool | `trustpilot` or `google`. Blank when none was found. |
| `checked` | tool | Date of the last lookup attempt, or blank for a site that has only been registered. |

`checked` MUST stay last. The Sheets API omits trailing empty cells, and when
every row comes back short pandas raises inside `GoogleSheetInterface.read()`
before this module sees it. `rating`, `reviews` and `source` are all blank for
a registered-but-unrated site, so `checked` is the only column that reliably
carries a value once a lookup has run.

That leaves one real exposure: a tab holding **only** placeholder rows has a
blank `checked` on every row. `RatingsTab.read()` therefore treats a read
failure the same way `ControlTab` does — it returns no ratings and logs, rather
than raising.

Registration and refresh both write through `write()`, which clears before it
writes, so both are **conditional on a successful read**: knowing which sites
are unseen requires knowing what is already there. A run that could not read
the tab writes nothing to it, which is the only safe response to a tab that
may still hold data we cannot see.

Every row is tool-owned, but the tab is deliberately human-legible: a rating
you disagree with can be corrected by hand, which matters when the model has
reported the same retailer three different ways.

## Refresh policy

A site needs a lookup when its `checked` date is absent, unparseable, or older
than `rating_rate_d` (default 30 days).

**At most one batch of `_RATING_BATCH` (10) sites per `update()`.** That bounds
the cost of any single run to one extra grounded call, whatever state the cache
is in — a first run against fifty retailers spreads over five days rather than
spending fifty searches at once. With a daily refresh and a handful of
retailers, the cache reaches steady state within a day or two.

**Every requested site has its `checked` stamped, whether or not a rating was
found.** Otherwise a retailer that genuinely has no Trustpilot or Google
presence would be looked up on every single refresh, forever. A blank rating
with a recent `checked` means "we asked, there was nothing" — which is a
different and much cheaper state than "we have not asked".

## The lookup

`RatingsLookup.fetch(sites: list[str]) -> dict[str, Rating]`, two calls in the
established shape — grounded search, then un-grounded JSON — reusing
`retrying`, `extract_json` and `FORMAT_PROVIDER`.

Call one names the retailers and asks for each one's Trustpilot rating out of 5
and its review count; Google's rating if there is no Trustpilot page, saying
which was used; and `unknown` rather than an estimate when neither is found.
The wording carries the same "do not estimate one" instruction the sweep prompt
uses today, for the same reason: an invented rating is worse than a missing
one, because a missing one is handled honestly as unrated.

Call two converts to:

```json
{"ratings": [{"site": "", "rating": 4.1, "reviews": 13305, "source": "trustpilot"}]}
```

`rating` and `reviews` are `null` when unknown. A returned site that was not
requested is ignored. A requested site that is not returned is stamped as
checked with no rating.

`MAX_RATING` (5.0) validation moves here, where the number enters the system: a
value outside `[0, 5]` is not a trustworthy five-point score and is stored as no
rating. `MIN_RATING_COUNT` (50) stays where it is, in `Candidate.rated` — it
governs whether a rating is *trusted*, which is a separate question from whether
it is *known*.

## Candidate construction

`market._candidates` stops reading `rating`, `rating_count` and `rating_source`
from the model's JSON. It looks them up instead:

```python
rating = ratings.get(supplier_domain(url))
```

`ratings` is passed into `price_market()` alongside `blocked`, which already
arrives that way. Everything downstream is unchanged: `.rated` still requires a
rating on at least `MIN_RATING_COUNT` reviews, `rating_label` still renders
`"4.1 Trustpilot"`, and `detections.select` still gates best-rated and
best-value on `.rated`.

A site with no cached rating is simply unrated. It competes on price and can be
the cheapest; it cannot be best-rated or best-value until the next refresh.

## Prompt changes

From `_MARKET_TEMPLATE`, the paragraph beginning "For each retailer also report
its Trustpilot rating" is removed. From `_MARKET_FORMAT_TEMPLATE`, the
`rating`, `rating_count` and `rating_source` fields and their rules are removed.

This is the point of the exercise. The sweep's instructions get shorter and its
grounding budget goes to shops.

## Config

```python
    ratings_sheet: str = "Ratings"
    # Ratings move over months while prices move daily, so the cache is
    # refreshed on its own slow cadence rather than on every sweep.
    rating_rate_d: float = 30.0
```

`_RATING_BATCH = 10` stays a module constant in `search/ratings.py` — it is a
number you would tune by editing the module and re-running its test, not from
JSON.

## Error handling

A ratings failure must never cost a sweep its prices.

| Failure | Behaviour |
| --- | --- |
| Ratings tab missing | Created empty, like `Control`; no ratings this run |
| Ratings tab unreadable | Logged and announced; no ratings this run; **never** overwritten, since `write()` clears before it writes |
| Lookup call raises after retries | Logged; the run proceeds on whatever is cached |
| Lookup returns unparseable JSON | Logged; no rows written; the sites stay stale and are retried next refresh |
| Registration write fails | Logged; the sites are registered on a later run |

## Testing

| Suite | Covers |
| --- | --- |
| `unit/test_ratings_tab.py` | Round-trip; `checked` last; a missing tab created; an unreadable tab neither overwritten nor fatal; registration appending only unseen sites and writing nothing when there are none |
| `unit/test_ratings.py` | Staleness selection (absent, unparseable and old dates all qualify); the one-batch-per-run cap; a rating outside `[0, 5]` stored as none; an unreturned site stamped as checked with no rating; unrequested sites ignored; failure paths returning empty rather than raising |
| `unit/test_market.py` | Candidates take their rating from the passed-in cache, not the payload; an uncached site is unrated but still competes on price |
| `unit/test_prompts.py` | The sweep templates no longer mention ratings |
| `unit/test_price_monitor.py` | Refresh runs before the sweeps; registration runs after; a ratings failure does not stop the run |

## What this costs

On the first run every retailer is unrated, so `rated_*` and `value_*` are
blank until the next update. The cache then fills at up to ten retailers per
day.

A stored rating can be a month old, and the model's fresher view is discarded in
its favour. That is the trade: stability and breadth, bought with currency. For
a number that moves over months and that we have watched change *source*
between two runs an hour apart, it is the right way round.

## Open risk

As with everything else on this branch, the real behaviour is unvalidated until
a live run. The specific thing to watch is whether removing the rating request
actually increases `sources` — the whole premise is that those grounding
passages get spent on shops instead, and that is a hypothesis, not a
measurement.
