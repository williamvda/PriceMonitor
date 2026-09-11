# PriceMonitor

PriceMonitor watches a list of products in a Google Sheet and records their
price over time, using a search-grounded LLM to look each one up. You add a
row, PriceMonitor finds the price, and both a running history and a
per-item summary are written back to the sheet — no scraper to maintain per
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

### The market sweep

An item with `market` set to `yes` gets a second, independent lookup each
run: a UK-wide sweep that asks what the item costs across retailers
generally, rather than at your one tracked supplier. It is recorded as its
own history row under the website `market`, so it keeps its own mean, its
own baseline and its own drop bands, entirely separate from the tracked
supplier's.

Each sweep asks for several retailers, not one — the others exist to make
the confidence score possible, not to be recorded. The cheapest candidate
that is in stock, priced in the configured currency, and not rated below 3.0
wins.

#### What confidence means

`confidence` answers "how much does this sweep support the claim that this
is the best UK price" — not "was this number read correctly". A sweep can
return a confident, well-formed, cheap price that turns out to belong to an
accessory, a used unit, or the wrong variant; confidence is the signal that
tells those two situations apart. It is built from four factors in `[0, 1]`,
**multiplied together**:

| Factor | Full marks | Penalised when |
| --- | --- | --- |
| Corroboration | three or more retailers found | fewer — a single hit is one shop found, not a market surveyed |
| Agreement | the winner is within 20% of the others' median price | it sits far below them, which usually means the wrong variant, an accessory, or a used unit |
| Sourcing | the winning URL is one the model actually cited | it is uncited, or there is no URL at all |
| Reputation | the retailer is rated 4.0 or better | it is rated lower, down to a 3.0 floor; an unrated retailer sits neutral rather than being punished |

Because the factors **multiply** rather than average, one bad signal drags
the whole score down whatever the others say. That is deliberate for a
number whose only job is to catch mistakes, not to reward an otherwise-good
sweep.

The reputation factor is worth a closer look, because it is the one most
likely to be mistaken for a bug. It is a **shoulder, not a cliff**: it stops
penalising at a rating of 4.0 and slides down linearly to a floor of 3.0,
below which a retailer is dropped from the sweep entirely before scoring
ever happens. A hard cutoff at 4.0 would sit right on top of several UK
majors — Argos (4.1), Currys (4.4), John Lewis (4.0) — so a fraction of a
point's movement in a public rating could flip a result in or out of your
sheet between sweeps for no reason connected to price. The shoulder degrades
confidence smoothly instead, and the `rating` column shows you the number
that drove it. A retailer with no rating at all, or too few reviews to mean
anything, sits neutral rather than being punished for being unrated.

A reading below `market_min_confidence` (default `0.5`) is still recorded,
still updates `best_price`, and still counts toward the market mean — it
simply does not trigger a drop notification, and the suppression is logged
so a repeatedly-gated item is visible rather than silently quiet. This
matters in one specific way: **corroboration alone caps a single-source
sweep at 0.33**, whatever else is true of it — a perfect price from a
perfectly-rated retailer that no one else stocks still scores 0.33. With the
default threshold at 0.5, that means an item only one UK retailer stocks
will *never* announce a market drop, however much its price falls. This is
expected behaviour, not a defect — the column is meant to answer "what's the
best price across all UK suppliers", and one shop answering is not a market.
If you deliberately track such niche items, lower `market_min_confidence` to
about 0.3 rather than changing anything about the scorer itself.

The `note` column on a market row always explains the score, so a low
confidence is never a mystery you have to re-run the lookup to understand,
e.g. `3 sources, winner 8% under median, cited, rated 4.4`.


#### When a sweep finds only one retailer

The `sources` column counts the retailers a sweep actually found — meaning
ones that survived every check: in stock, priced in your currency, and not
below the reputation floor. A model that names five retailers of which four
are out of stock leaves `sources` at 1, because one shop is all that can
actually sell you the thing.

A `1` here is worth acting on. It caps confidence at 0.33, which with the
default `market_min_confidence` means that item will never announce a drop —
so the sweep is costing you a billed search every run and can never tell you
anything. Usually it means the product name is too specific or too obscure to
match across retailers. Either broaden the name in the `item` column, point
`website` at a supplier you actually want tracked and leave the sweep to do
less work, or turn `market` off for that row.

Sort or filter the Items tab on `sources`, or conditional-format `=1`, to find
these rows at a glance. A blank means the item has never been swept; a `0`
never appears, because a sweep that found nothing records no count at all —
`status` on the Prices tab says what went wrong instead.

## The sheet

PriceMonitor expects a Google Sheet with two tabs, named by `items_sheet`
and `history_sheet` in `price_ctrl` (`Items` and `Prices` by default).

### Items tab

You own columns A through C; everything after them is rewritten on every run.

| Column         | Owner  | Meaning                                                                 |
|----------------|--------|--------------------------------------------------------------------------|
| `item`                  | you  | The product name, used for display and as part of its identity.        |
| `website`               | you  | Either a bare domain (e.g. `shop.example`) to search, or a full product page URL to read directly. |
| `market`                | you  | `yes` (also `y`, `true`, or `1`, case-insensitive) to also sweep UK retailers generally for this item on every run. Blank, or anything else, means no. Each sweep costs one extra billed search — see [The market sweep](#the-market-sweep). |
| `current`               | tool | Most recent successfully-priced value.                                 |
| `min`                   | tool | Lowest price seen.                                                     |
| `mean`                  | tool | Mean of all successfully-priced values.                                |
| `last_checked`          | tool | Timestamp of the most recent attempt, successful or not. Reads `never` for an item with no readings yet — never a blank cell, since a blank cell is ambiguous between "not checked yet" and "something went wrong". |
| `best_price`            | tool | The cheapest price found by the most recent market sweep. Blank if `market` is not `yes`. |
| `best_supplier`         | tool | The domain of the retailer behind `best_price`.                        |
| `best_supplier_rating`  | tool | That retailer's review rating and source, e.g. `4.1 Trustpilot`; blank if unrated. |
| `confidence`            | tool | The market sweep's confidence score, `0`–`1`. See [What confidence means](#what-confidence-means). |
| `sources`               | tool | How many retailers the last sweep actually found — see [When a sweep finds only one retailer](#when-a-sweep-finds-only-one-retailer). Blank if never swept. |
| `market_checked`        | tool | Timestamp of the most recent sweep attempt. Reads `never` for an item never swept — including every item with `market` left blank. |

Blank or duplicate `(item, website)` rows are skipped and logged, not sent
to the LLM.

### Prices tab (history)

Append-only: one row is added per item on every run that prices it,
regardless of outcome. Columns: `timestamp`, `item`, `website`, `price`,
`currency`, `source_url`, `note`, `confidence`, `rating`, `sources`, `status`.

A market sweep is recorded here too, under the website `market` rather than
your tracked supplier's domain — so an item with `market` set to `yes` gets
two rows per run, each keeping its own history, mean and drop bands. Only
market rows populate `confidence` and `rating`; both are blank on ordinary
supplier rows.

`status` is one of:

| Status           | Meaning                                                                 |
|------------------|--------------------------------------------------------------------------|
| `ok`             | A price was found and looks plausible.                                 |
| `suspect`        | A price was found, but it moved by more than `suspect_threshold` from the last recorded price — recorded, but worth a look. |
| `not_found`      | The model could not find the product/price on the site or page.        |
| `wrong_currency` | The price was reported in a currency other than the configured one.    |
| `rejected`       | The reported price failed a sanity check (not a number, zero or negative, above `max_plausible_price`, etc.). |
| `parse_error`    | The model's reply could not be parsed into the expected JSON shape.    |
| `error`          | The lookup itself failed (network, API error, exception) before a price could even be attempted. |

Only `ok` and `suspect` rows carry a non-null `price`; every other status
records `price` as blank.

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
`Items` tab yourself and add your rows (`item`, `website`) to get started.

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
