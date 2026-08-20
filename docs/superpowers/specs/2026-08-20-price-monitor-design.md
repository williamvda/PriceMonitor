# PriceMonitor — Design

Date: 2026-08-20

## Purpose

Track the price of a user-maintained list of items over time. A long-lived
process reads items from a Google Sheet, asks a web-search-backed LLM for each
item's current price, appends every reading to a history tab, and writes
current/min/mean back onto the items tab. Items added to the sheet are picked
up and priced within minutes without a restart.

## Dependencies

| Package | Import | Source |
| --- | --- | --- |
| `py-utils` | `py_utils` | `git+https://github.com/williamvda/py-utils.git@v0.1.1` |
| `google-drive-api` | `google_drive_api` | `git+https://github.com/williamvda/google-drive-api.git@v0.1.2` |
| `llmbridge` | `llmbridge` | local editable — see below |
| `pandas`, `python-dotenv` | | PyPI |

`llmbridge` (the `AIInterface` repo) has no git remote and no tags, so it
cannot be pinned by URL. It is listed as a bare name in `dependencies` and must
be installed first:

```bash
pip install -e ../AIInterface
```

Once that repo is pushed and tagged, this becomes a one-line change to a
pinned `git+https://…@vX.Y.Z` reference, matching the other two.

## Architecture

```
src/price_monitor/
  price_monitor.py     # controller: thread, two timers, orchestration, main()
  app_config.py        # config dataclasses used as load_config schema
  models.py            # Item, PriceReading, PriceStatus
  search/
    gemini_search.py   # GeminiSearchProvider subclass + registration
    searcher.py        # PriceSearcher: prompt -> LLMClient -> parse -> validate
  sheets/
    items_tab.py       # read items, detect new, write summary columns
    history_tab.py     # append readings, compute current/min/mean
tests/unit/
tests/integration/
```

Boundaries: `PriceMonitor` orchestrates and owns timing; it never parses JSON
or addresses sheet ranges. `PriceSearcher` knows nothing about sheets. The two
sheet modules know nothing about LLMs. Each is testable against a fake
collaborator with no network.

## Data model

```python
class PriceStatus(str, Enum):
    OK = "ok"                       # clean reading
    SUSPECT = "suspect"             # recorded, but moved > threshold vs last
    NOT_FOUND = "not_found"         # searched, no price found
    WRONG_CURRENCY = "wrong_currency"
    REJECTED = "rejected"           # <= 0 or above max_plausible_price
    PARSE_ERROR = "parse_error"     # reply was not usable JSON
    ERROR = "error"                 # LLMError / network failure


@dataclass(frozen=True)
class Item:
    name: str
    website: str            # domain to search, or an exact product URL

    @property
    def is_direct_url(self) -> bool:
        """True when website points at a specific product page, not just a site."""


@dataclass(frozen=True)
class PriceReading:
    timestamp: datetime
    item: str
    website: str            # the configured target, as written in the sheet
    price: float | None
    currency: str
    status: PriceStatus
    source_url: str         # where the model actually read the price
    note: str = ""
```

`Item.is_direct_url` is mechanical: strip any `http(s)://` scheme, split on the
first `/`; a non-empty remainder means a product page, otherwise a site to
search. `www.amazon.co.uk` and `https://amazon.co.uk` are both sites;
`https://www.amazon.co.uk/dp/B09XS7JWHH` is a product page.

Item identity is the `(name, website)` pair.

## Sheet contract

**Items tab** (default name `Items`). Columns A–B are user-owned; C–F are
rewritten by each run.

| item | website | current | min | mean | last_checked |

**Prices tab** (default name `Prices`). Append-only, one row per item per run.

| timestamp | item | website | price | currency | status | source_url | note |

`website` and `source_url` are deliberately distinct: the former is what was
configured, the latter is where the price was actually read. When `website` is
a bare domain, that pairing is the entire audit trail for a reading, and it is
what makes a `SUSPECT` row diagnosable.

### Write mechanics

`GoogleSheetInterface` exposes no ranged append, so both tabs are rewritten
from a full DataFrame each time. The two tabs use different methods, for
different reasons:

- **Prices → `update()`** (writes from A1 without clearing). The tab is
  append-only and never shrinks, so no clear is needed — and `write()`'s
  clear-then-write sequence opens a window in which a crash would destroy the
  entire price history.
- **Items → `write()`** (clear, then write). This tab *can* shrink when a row
  is deleted; without the clear, a deletion would leave a duplicated trailing
  row.

This asymmetry is non-obvious and must carry a comment in the code.

## Search adapter — two-call design

Combining Google Search grounding with structured JSON output is unreliable
across the Gemini model line, not merely on one model. Documented failures
include the leading segment of the response text being dropped so the JSON
never parses, empty `response.text` under grounding, truncation reported with
`finishReason: "STOP"`, and — critically for this design —
`groundingChunks`/`groundingSupports` coming back **empty** when structured
output is requested, which would destroy the `source_url` audit trail.

PriceMonitor therefore splits each lookup into two calls:

**Call 1 — grounded search.** Uses `GeminiSearchProvider` and asks for a plain
prose answer. No structured output is requested, so grounding metadata stays
populated.

```python
class GeminiSearchProvider(GeminiProvider):
    PROVIDER_NAME = "gemini_search"

    def build_request_body(...):   # adds tools: [{"google_search": {}}]
    def parse_response(...):       # joins every parts[i]["text"], skips non-text parts

ProviderRegistry.register("gemini_search", GeminiSearchProvider)
```

**Call 2 — formatting.** Uses `llmbridge`'s **stock** `GeminiProvider`, with no
tools and no grounding, to convert call 1's prose into the JSON object below.
This is the well-supported regime, so no subclass is needed for it.

`PriceSearcher` therefore holds two `LLMClient` instances — one bound to
`gemini_search`, one to `gemini` — both built from the same `llm_config`.

Verified against live documentation on 2026-08-20: the grounding tool key on
`v1beta/models/{model}:generateContent` is `{"google_search": {}}` (older
models used `google_search_retrieval`), answer text is at
`candidates[0].content.parts[*].text`, and grounding sources are at
`candidates[0].groundingMetadata.groundingChunks[*].web.uri`.

`parse_response` **must** be overridden on the search provider: the base
implementation indexes the first part only
(`candidate["content"]["parts"][0]["text"]`), which truncates the answer once
grounding splits it across several parts.

Adding Anthropic or OpenAI later is the same subclass registered under
`anthropic_search` / `openai_search`, selected purely by the
`llm_config.provider` string. No PriceMonitor code branches on provider. Local
models (Ollama) have no server-side search and cannot be supported this way.

### Source URL provenance

`source_url` is taken from the JSON `url` field when call 2 supplies one. When
it is blank, it falls back to the first
`groundingMetadata.groundingChunks[*].web.uri` read out of call 1's
`PromptResponse.raw_response`, which `llmbridge` preserves unmodified. Only if
both are absent is `source_url` left blank.

## Response contract and parsing

Call 2 is prompted for a bare JSON object. No response mime type is set — the
stock provider does not expose one, and prompt-level JSON is reliable without
grounding in play:

```json
{
  "price": 279.00,
  "currency": "GBP",
  "url": "https://…",
  "in_stock": true,
  "found": true,
  "note": ""
}
```

`searcher.py` extracts it tolerantly: strip markdown fences, then scan for the
first balanced `{…}`. As a last resort, if a closing `}` is present but no
opening `{`, prepend one and retry — this recovers the known leading-truncation
failure should it ever surface on call 2. The extractor is the highest-risk
function in the project and gets dedicated unit tests for fenced,
prose-wrapped, leading-truncated, and malformed replies. Extraction failure
produces a `PARSE_ERROR` row; it never raises out of the run.

Two call-1 prompt templates, selected by `Item.is_direct_url`: read the price
from an exact page, or search a site for a named item. Each is unit-tested.

An empty or whitespace-only reply from either call is treated as
`PARSE_ERROR`, covering the documented empty-`response.text` failure.

## Validation ladder

Applied in order, first match wins:

1. no parseable JSON → `PARSE_ERROR`, price blank
2. `found` is false → `NOT_FOUND`, price blank, model's reason into `note`
3. `currency` != configured currency → `WRONG_CURRENCY`, price blank
4. `price <= 0` or `price > max_plausible_price` → `REJECTED`, price blank
5. a last known price exists and `abs(price - last) / last > suspect_threshold`
   → `SUSPECT`, **price still recorded**
6. otherwise → `OK`

"Last known price" means the most recent non-null price for that `(name,
website)` pair in the history tab. When an item has no prior reading, step 5
cannot fire and a first reading is never `SUSPECT`.

## Summary statistics

Computed per item over every history row for that item with a non-null price:

- `current` — the price of the most recent such reading
- `min` — the lowest such price
- `mean` — the arithmetic mean of such prices
- `last_checked` — the timestamp of that item's most recent reading of *any*
  status, so a run of failures is visible as a stale-but-moving timestamp
  rather than a frozen one

`SUSPECT` readings carry a price and therefore count toward all three
statistics; they are flagged in their own history row rather than silently
absorbed. An item with no non-null price anywhere leaves `current`/`min`/`mean`
blank.

## Run loop

One thread, one `stop_event`, two independent timers — the same shape as
`FinanceMonitor`:

- **every `refresh_rate_h` (default 6)** — price every item, append the
  readings, rewrite the Items summary.
- **every `poll_rate_m` (default 5)** — call `is_modified()` (a cheap Drive
  metadata call). If the file changed, read the Items tab and diff against an
  in-memory set of known items, seeded at startup from the Prices tab. Price
  **only** the new items, append, rewrite the summary.

This satisfies the requirement that a newly added item is priced automatically
rather than waiting for the next six-hour tick, while costing nothing in
LLM/search spend when the sheet is untouched.

On startup a full refresh fires immediately rather than waiting out the first
six-hour interval, so a restart produces a visible result at once.

`temperature` is 0.0 for repeatability.

### Malformed item rows

Rows read from the Items tab are validated before any request is made. A row
with a blank `item` or blank `website` is skipped with a logged warning and no
history row. Duplicate `(name, website)` pairs are de-duplicated, keeping the
first occurrence, so a copy-pasted row cannot double the search spend.

## Error handling

- A single item's failure yields an `ERROR` row and the run continues. One
  unreachable site never blocks the rest.
- `LLMError` and its subclasses (auth, rate limit, connection, response) are
  caught per item.
- The update and poll calls are each wrapped in try/except inside the loop, so
  a failed run logs a warning and never kills the thread.
- `request_delay_s` spaces out per-item requests to stay clear of rate limits.
- All state lives in the sheet, so a restart loses nothing.

## Configuration

`--secrets <dir>` containing `config.json` and `.env`, following
`FinanceMonitor`. `ENCRYPTION_KEY` in `.env` decrypts `EncStr` fields.

```json
{"config": {
  "drive_config": {
    "service_file": "service_account.json",
    "remote_file": "PriceMonitor"
  },
  "llm_config": {
    "provider": "gemini_search",
    "model": "gemini-3.7-flash",
    "api_key": "<EncStr>",
    "max_tokens": 1024,
    "temperature": 0.0
  },
  "price_ctrl": {
    "refresh_rate_h": 6,
    "poll_rate_m": 5,
    "items_sheet": "Items",
    "history_sheet": "Prices",
    "currency": "GBP",
    "suspect_threshold": 0.5,
    "max_plausible_price": 100000,
    "request_delay_s": 2
  }
}}
```

`drive_config` reuses `google_drive_api.DriveConfig`. Every `price_ctrl` and
`llm_config` field carries a default so a missing section still loads.

`provider` names the **call 1** (grounded) provider. Call 2 always uses the
plain `gemini` provider with the same `model` and `api_key`. A flash model is
the default: this task reads a number off a page and is not reasoning-limited,
and pro models show their own grounding failures without buying reliability.

## CLI

```bash
price-monitor --secrets ~/secrets          # run the daemon
price-monitor --secrets ~/secrets --once   # single update, then exit
```

`args_parser()` is a standalone function; `main()` creates the controller,
calls `start()`, joins, and handles `KeyboardInterrupt` — per project
convention. `--once` exists so a run can be exercised without waiting on
timers.

## Testing

Test-driven throughout. Unit tests use a fake `LLMClient` and a fake
`GoogleSheetInterface`; no test touches the network.

Coverage targets:

- JSON extraction: fenced, prose-wrapped, leading-truncated, malformed, empty
- every branch of the validation ladder
- `Item.is_direct_url` across domain / scheme / product-URL forms
- prompt template selection
- `GeminiSearchProvider` body injection and multi-part response parsing
- the two-call sequence: call 1's prose is what reaches call 2, and a call 1
  failure short-circuits without making call 2
- `source_url` fallback to `groundingChunks[*].web.uri` when the JSON `url` is
  blank
- current/min/mean computation, including rows with null prices
- new-item detection, including an item added and removed between polls
- malformed item rows: blank name, blank website, duplicate pairs
- DataFrame shaping for both tabs

Integration tests (`-m integration`, opt-in, excluded by default via
`addopts`): one real grounded search, one real sheet round-trip.

## Out of scope

- Providers other than Gemini (the adapter makes them cheap to add later)
- Scrape-and-extract fallback when a search returns nothing
- Alerting or notification on price drops
- Multi-currency support within one sheet
- Backfilling history for items added later
