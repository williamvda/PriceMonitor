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

## The sheet

PriceMonitor expects a Google Sheet with two tabs, named by `items_sheet`
and `history_sheet` in `price_ctrl` (`Items` and `Prices` by default).

### Items tab

You own columns A and B; everything after them is rewritten on every run.

| Column         | Owner  | Meaning                                                                 |
|----------------|--------|--------------------------------------------------------------------------|
| `item`         | you    | The product name, used for display and as part of its identity.        |
| `website`      | you    | Either a bare domain (e.g. `shop.example`) to search, or a full product page URL to read directly. |
| `current`      | tool   | Most recent successfully-priced value.                                 |
| `min`          | tool   | Lowest price seen.                                                     |
| `mean`         | tool   | Mean of all successfully-priced values.                                |
| `last_checked` | tool   | Timestamp of the most recent attempt, successful or not. Reads `never` for an item with no readings yet — never a blank cell, since a blank cell is ambiguous between "not checked yet" and "something went wrong". |

Blank or duplicate `(item, website)` rows are skipped and logged, not sent
to the LLM.

### Prices tab (history)

Append-only: one row is added per item on every run that prices it,
regardless of outcome. Columns: `timestamp`, `item`, `website`, `price`,
`currency`, `source_url`, `note`, `status`.

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
The client is an optional dependency, so install the extra:

```bash
pip install price-monitor[msgserver] @ git+ssh://git@github.com/williamvda/PriceMonitor.git@v0.1.0
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
