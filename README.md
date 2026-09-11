# PriceMonitor

PriceMonitor watches a list of products in a Google Sheet and, on every run,
searches UK retailers generally rather than checking one shop you named. For
each item it reports three answers — the best-rated retailer, the cheapest,
and a best-value compromise between them — using a search-grounded LLM to do
the finding. You add a row with just the product's name; PriceMonitor writes
back a running history and a per-item summary, no scraper to maintain per
site.

## How it works

Two independent timers drive the controller:

- **Full refresh** (`refresh_rate_h`, default 6 hours) — prices every item on
  the Items tab and rewrites the summary.
- **Poll** (`poll_rate_m`, default 5 minutes) — checks whether the sheet has
  changed since the last check and, if so, prices only the items that are new
  since the last full refresh (i.e. not yet present in the history). It never
  re-prices an item the refresh already covers.

A full refresh always fires once immediately on startup, rather than waiting
out the first interval — so adding items and starting PriceMonitor gets you
prices right away.

Each item is priced independently. If one lookup fails (site unreachable,
the model errs, an unparseable reply), that item gets an `error` row and the
run continues with the rest — a single bad item never aborts the batch, and
a bad batch never kills the process (it's logged and retried on the next
cycle).

### The two-call lookup

Each price lookup is two LLM calls, not one:

1. A search-grounded call asks the model to find and report the product's
   price in prose, with the page it read it from.
2. An un-grounded call converts that prose report into structured JSON.

This is deliberate: Gemini's search grounding and structured/JSON output are
unreliable together, so grounding is used only for the call that needs to
search, and the JSON call runs with grounding switched off.

### Every item, one sweep

Each run does one thing per item: a UK-wide sweep that asks what the product
costs across retailers generally, not at one shop you named. There is no
per-supplier lookup any more — the sweep is the only kind of lookup
PriceMonitor makes, and its cost (one billed search) is the cost of pricing
that item at all.

Each sweep asks for several retailers, not one — the others exist to make
the confidence score possible and to give the three detections below
something to choose between. A candidate must be in stock, priced in the
configured currency, and pass validation to be counted at all.

### The three detections

A sweep answers three separate questions, each recorded as its own price,
site and rating:

| Detection | Question it answers |
| --- | --- |
| **Best-rated** | Of the retailers with a trustworthy rating, which is rated highest? |
| **Cheapest** | Of every candidate that survived, which is cheapest? |
| **Best value** | Of the rated retailers, which is the best compromise between price and rating? |

Only the cheapest series is tracked over time — `min`, `mean` and drop
notifications all run against `cheapest_price` alone, because it is the one
quantity with a meaningful trend. The best-rated retailer's price says more
about that retailer than about the market, and the best-value pick moves
with whichever candidates a given sweep happened to return, so neither would
give a stable series worth comparing against itself.

#### The best-value rule

Best value is computed only over *rated* candidates, using the cheapest
rated one as the baseline. A candidate rated higher than the baseline
qualifies when its price premium fits inside a budget its rating gain earns:
each rating point above the baseline is worth spending up to
`rating_premium_pct` (default `0.05`, i.e. 5%) of the baseline price. Among
the candidates that qualify, the highest-rated one wins; if none qualify,
the baseline itself is the answer — "nothing better-rated is worth the
money" is a real result, not a failure.

Worked example, at the default `rating_premium_pct` of `0.05`:

| Retailer | Price | Rating |
| --- | --- | --- |
| A (unrated) | £85 | — |
| B | £100 | 4.0 |
| C | £101 | 4.4 |
| D | £110 | 4.8 |

B is the cheapest *rated* candidate, so it is the baseline: £100 at 4.0.
Each rating point above 4.0 is worth up to £5 (5% of £100):

- C is 0.4 points above the baseline, earning a budget of £2. Its actual
  premium is £1, so it qualifies.
- D is 0.8 points above, earning a budget of £4. Its actual premium is £10,
  so it does not qualify.

C is the only qualifier, so it wins best value. The sweep's three detections
end up as: best-rated = D (£110, 4.8), cheapest = A (£85, unrated), best
value = C (£101, 4.4).

Notice that `value_price` (£101) is *higher* than `cheapest_price` (£85).
This is expected, not a bug — it is the single most likely thing to be
reported as one. The baseline for the best-value rule is the cheapest
**rated** candidate, because an unrated baseline has no rating to improve
on; treating an unrated price as rating zero would make every rated
retailer look like a bargain worth a large premium, which inverts the
rule's purpose. So whenever the overall cheapest retailer is unrated,
`value_price` can exceed `cheapest_price` — you are paying a premium for a
known retailer over an unknown one, which is the point of the column
existing.

#### What confidence means

`confidence` answers "how much does this sweep support the claim that this
is the best UK price" — not "was this number read correctly". A sweep can
return a confident, well-formed, cheap price that turns out to belong to an
accessory, a used unit, or the wrong variant; confidence is the signal that
tells those two situations apart. It is built from three factors in `[0, 1]`,
**multiplied together**:

| Factor | Full marks | Penalised when |
| --- | --- | --- |
| Corroboration | three or more retailers found | fewer — a single hit is one shop found, not a market surveyed |
| Agreement | the cheapest candidate is within 20% of the others' median price | it sits far below them, which usually means the wrong variant, an accessory, or a used unit |
| Sourcing | the winning URL is one the model actually cited | it is uncited, or there is no URL at all |

Because the factors **multiply** rather than average, one bad signal drags
the whole score down whatever the others say. That is deliberate for a
number whose only job is to catch mistakes, not to reward an otherwise-good
sweep.

The `note` column always explains the score, so a low confidence is never a
mystery you have to re-run the lookup to understand, e.g. `3 sources,
winner 8% under median, cited`.

#### Ratings are shown, not scored

An earlier version of this scorer included a reputation factor that
penalised retailers below a rating threshold. That factor is gone.
Trustpilot ratings cannot distinguish "an enormous retailer that a lot of
people happen to complain about" from "a shop you should avoid" — Amazon UK
sits at 1.4 out of 5 from 33,904 Trustpilot reviews, a number driven by
delivery-adjacent complaints and review volume, not by whether Amazon is a
legitimate place to buy something. Scoring that number would have excluded
Amazon from every sweep it appeared in, which is the wrong outcome for a
scorer meant to catch fabricated prices, not to grade customer service.

So a rating is shown against every detection — `rated_rating`,
`cheapest_rating`, `value_rating` — for you to read and judge, but it plays
no part in `confidence`. A rating needs at least 50 reviews behind it to be
trusted at all; fewer than that, or none found, and the column is left
blank rather than guessed at.

A reading below `min_confidence` (default `0.5`) is still recorded, still
updates every column, still counts toward the cheapest mean — it simply
does not trigger a drop notification, and the suppression is logged so a
repeatedly-gated item is visible rather than silently quiet. This matters
in one specific way: **corroboration alone caps a single-source sweep at
0.33**, whatever else is true of it — a perfect price from a single
retailer that no one else stocks still scores 0.33. With the default
threshold at 0.5, that means an item only one UK retailer stocks will
*never* announce a price drop, however much its price falls. This is
expected behaviour, not a defect — the column is meant to answer "what's
the best price across all UK suppliers", and one shop answering is not a
market. If you deliberately track such niche items, lower `min_confidence`
to about 0.3 rather than changing anything about the scorer itself.

#### When a sweep finds only one retailer

The `sources` column counts the retailers a sweep actually found — meaning
ones that survived every check: in stock, priced in your currency, and
validated. A model that names five retailers of which four are out of
stock leaves `sources` at 1, because one shop is all that can actually
sell you the thing.

A `1` here is worth acting on. It caps confidence at 0.33, which with the
default `min_confidence` means that item will never announce a drop — so
the sweep is costing you a billed search every run and can never tell you
anything. Usually it means the product name is too specific or too obscure
to match across retailers — broadening the name in the `item` column is the
first thing to try. Failing that, lower `min_confidence` if you want to keep
tracking such a niche item anyway.

Sort or filter the Items tab on `sources`, or conditional-format `=1`, to find
these rows at a glance. A blank means the item has never been swept; a `0`
never appears, because a sweep that found nothing records no count at all —
`status` on the Prices tab says what went wrong instead.

## The sheet

PriceMonitor expects a Google Sheet with two tabs, named by `items_sheet`
and `history_sheet` in `price_ctrl` (`Items` and `Prices` by default).

### Items tab

Column A (`item`) is yours; every column after it is rewritten on every run.

| Column | Owner | Meaning |
| --- | --- | --- |
| `item` | you | The product name — the only thing you type. |
| `rated_price` | tool | The best-rated detection's price. Blank if nothing rated. |
| `rated_site` | tool | Its retailer's domain. |
| `rated_rating` | tool | Its rating and source, e.g. `4.4 Trustpilot`. |
| `cheapest_price` | tool | The cheapest detection's price. |
| `cheapest_site` | tool | Its retailer's domain. |
| `cheapest_rating` | tool | Its rating; blank when that retailer is unrated. |
| `value_price` | tool | The best-value detection's price. See [The best-value rule](#the-best-value-rule) — this can exceed `cheapest_price`. |
| `value_site` | tool | Its retailer's domain. |
| `value_rating` | tool | Its rating. |
| `min` | tool | Lowest `cheapest_price` ever recorded. |
| `mean` | tool | Mean of all recorded `cheapest_price` values. |
| `confidence` | tool | The latest sweep's confidence score, `0`–`1`. See [What confidence means](#what-confidence-means). |
| `sources` | tool | How many retailers the latest sweep actually found — see [When a sweep finds only one retailer](#when-a-sweep-finds-only-one-retailer). Blank if never swept. |
| `last_checked` | tool | Timestamp of the latest sweep, any status. Reads `never` for an item never swept — never a blank cell, since a blank cell is ambiguous between "not checked yet" and "something went wrong". |

Duplicate `item` values are skipped and logged, not sent to the LLM.

### Prices tab (history)

Append-only: one row is added per item on every run, regardless of outcome.
Columns: `timestamp`, `item`, `rated_price`, `rated_site`, `rated_rating`,
`cheapest_price`, `cheapest_site`, `cheapest_rating`, `value_price`,
`value_site`, `value_rating`, `confidence`, `sources`, `note`, `status`.

A failed sweep still gets a row — all nine detection cells, `confidence` and
`sources` blank, `status` and usually `note` explaining what happened. Only
`cheapest_price` feeds `min`, `mean` and drop notifications on the Items
tab; `rated_price` and `value_price` are recorded and shown but never
tracked over time.

`status` is one of:

| Status        | Meaning                                                                 |
|---------------|--------------------------------------------------------------------------|
| `ok`          | A price was found and looks plausible.                                 |
| `suspect`     | A price was found, but the cheapest moved by more than `suspect_threshold` from the last recorded cheapest price — recorded, but worth a look. |
| `not_found`   | The sweep produced no usable candidate.                                |
| `parse_error` | The model's reply could not be parsed into the expected JSON shape.    |
| `error`       | The lookup itself failed (network, API error, exception) before a price could even be attempted. |

Only `ok` and `suspect` rows carry a non-blank `cheapest_price`; every other
status leaves all three detections blank.

### Migrating from the per-supplier version

**The Items tab migrates itself.** `ItemsTab.read()` looks up the `item`
column by name and ignores the stale `website` and `market` columns
entirely; the first summary write rewrites the whole tab in the new shape.
Your item list survives untouched — no manual step needed.

**You must delete the Prices tab yourself before the first run.**
PriceMonitor recreates it on first append, but it will not drop a history
tab on its own — that tab is your data, it cost real API calls to
accumulate, and a tool willing to silently discard a history tab is not one
you can trust with the next one. The old rows measure a different quantity
(a per-supplier price, not a UK-wide cheapest) and left in place they would
be reindexed under the new columns with every price blank — harmless to the
statistics, since a blank `cheapest_price` is excluded from them, but
confusing to read.

## Installation

Three dependencies — `py-utils`, `google-drive-api`, and `llmbridge` (from the
`AIInterface` repo) — live in private GitHub repositories and are pinned to
tags over SSH. The installing machine therefore needs an SSH key with **read
access to all three**; a GitHub deploy key will not do, since a deploy key is
scoped to a single repository. Use a machine user, or an agent-forwarded
personal key.

```bash
pip install ".[dev]"        # development, from a checkout
pip install git+ssh://git@github.com/williamvda/PriceMonitor.git@v0.1.0
```

The MsgServer client is an optional dependency needed only for `--msg-server`.
Extras go on the whole requirement, and the string needs quoting for the shell:

```bash
pip install ".[dev,msgserver]"   # development, from a checkout
pip install "price-monitor[msgserver] @ git+ssh://git@github.com/williamvda/PriceMonitor.git@v0.1.0"
```

Verify the SSH access before installing, or pip will fail partway through with
a bare git error:

```bash
for r in py-utils google-drive-api AIInterface PriceMonitor; do
  git ls-remote "git@github.com:williamvda/$r.git" >/dev/null \
    && echo "$r ok" || echo "$r UNREACHABLE"
done
```

The pins are tags, never branches — a redeploy must never be able to pull
dependency code that no test has run against. When you release a new version
of a dependency, bump the tag in `pyproject.toml` deliberately and re-run the
suites against a clean virtualenv built from the pins alone, not from editable
installs of your local checkouts.

## Setup

### 1. A Google Cloud service account

Create a service account with the Drive and Sheets APIs enabled, download
its JSON key file, and note its `client_email` — you'll share the
spreadsheet with that address.

### 2. The spreadsheet

Create a Google Sheet and share it (Editor access) with the service
account's email address. PriceMonitor will create the `Items` and `Prices`
tabs on first write if they don't already exist; you only need to create the
`Items` tab yourself and add your rows (just `item`) to get started.

### 3. The secrets directory

PriceMonitor reads everything it needs from one directory, passed via
`--secrets`:

```
<secrets>/
  config.json
  .env
  service_account.json
```

`.env` **must** define `ENCRYPTION_KEY`:

```
ENCRYPTION_KEY=<your Fernet key>
```

This is not optional. `api_key` in `config.json` is decrypted in *strict*
mode: PriceMonitor treats it as a value that is always encrypted, so a
missing `ENCRYPTION_KEY`, a wrong key, or a plaintext `api_key` all raise a
`DecryptionError` on startup rather than silently working (or silently
leaking a plaintext key). See `config/config.example.json` for the shape of
`config.json` — copy it into your secrets directory and fill in real values.
Its `api_key` field is a placeholder only; it is not a working value and
will not decrypt.

#### Encrypting the API key

Generate a Fernet key once, put it in `.env`, and use it to encrypt your
real LLM API key into the token that `config.json` expects:

```python
from cryptography.fernet import Fernet

key = Fernet.generate_key()
print(key.decode())  # put this in ENCRYPTION_KEY in .env
```

```python
import os
from py_utils.config import Encryptor

os.environ["ENCRYPTION_KEY"] = "<the key printed above>"
token = Encryptor().enc_str("<your real Gemini API key>")
print(token)  # put this in config.json's llm_config.api_key
```

## Running

```bash
price-monitor --secrets /path/to/secrets
```

Starts the timer loop described above and runs until interrupted
(`Ctrl+C`).

```bash
price-monitor --secrets /path/to/secrets --once
```

Runs a single full refresh and exits — useful for testing a config or
running from an external scheduler instead of PriceMonitor's own loop.
