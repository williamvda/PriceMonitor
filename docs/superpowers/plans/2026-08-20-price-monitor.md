# PriceMonitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A long-lived process that reads tracked items from a Google Sheet, prices each one via a web-search-grounded LLM, appends every reading to a history tab, and writes current/min/mean back to the items tab.

**Architecture:** Three isolated layers. `search/` turns an `Item` into a `PriceReading` using a two-call LLM sequence (grounded search, then un-grounded JSON formatting) and knows nothing about sheets. `sheets/` reads items and appends readings and knows nothing about LLMs. `price_monitor.py` owns timing and orchestration and parses neither JSON nor sheet ranges.

**Tech Stack:** Python 3.11, pandas, `llmbridge` (LLM REST client), `google-drive-api` (Sheets), `py-utils` (config/encryption/logging), pytest, black, isort, flake8.

**Spec:** `docs/superpowers/specs/2026-08-20-price-monitor-design.md`

## Global Constraints

- Python `>=3.11`. PEP 604 unions (`X | None`); never `Optional[X]`; no `from __future__ import annotations`.
- Annotate all function signatures and dataclass fields.
- `@dataclass(frozen=True)` for value objects; plain `@dataclass` for config.
- Every optional config field carries a `default` or `default_factory` so a missing JSON section does not raise.
- `pathlib.Path` for all paths, never bare strings.
- `logger.info/warning/error` only — never `print()`, except the `KeyboardInterrupt` message in `main()`.
- Every `.py` file opens with a one-paragraph module docstring.
- Inline comments only for non-obvious invariants — do not narrate the code.
- `class X(str, Enum)` so members serialise to strings directly.
- Imports ordered stdlib → third-party → local. No wildcards.
- Format with black (line-length 88), sort imports with isort (profile black), lint with flake8.
- Test layout: `tests/unit/` (fast, no network), `tests/integration/` (opt-in, `-m integration`).
- Timestamp format everywhere: `"%Y-%m-%d %H:%M:%S"`.
- `llmbridge` import name is `llmbridge` (the package was renamed from `llm_client`).

---

### Task 1: Project scaffolding and value objects

**Files:**
- Create: `pyproject.toml`
- Create: `src/price_monitor/__init__.py`
- Create: `src/price_monitor/models.py`
- Create: `tests/__init__.py`, `tests/unit/__init__.py`
- Test: `tests/unit/test_models.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `PriceStatus` (str Enum), `Item(name: str, website: str)` with `is_direct_url: bool` property, `PriceReading(timestamp: datetime, item: str, website: str, price: float | None, currency: str, status: PriceStatus, source_url: str = "", note: str = "")`

- [ ] **Step 1: Create the package skeleton and pyproject**

```bash
mkdir -p src/price_monitor/search src/price_monitor/sheets tests/unit tests/integration
touch tests/__init__.py tests/unit/__init__.py tests/integration/__init__.py
```

`pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "price-monitor"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "py-utils @ git+https://github.com/williamvda/py-utils.git@v0.1.1",
    "google-drive-api @ git+https://github.com/williamvda/google-drive-api.git@v0.1.2",
    "llmbridge",
    "pandas",
    "python-dotenv",
]

[project.scripts]
price-monitor = "price_monitor.price_monitor:main"

[project.optional-dependencies]
dev = ["pytest>=8.0", "black>=24.0", "isort>=5.13", "flake8>=7.0"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
addopts = "-v -m 'not integration'"
markers = [
    "integration: live tests against external APIs (run with -m integration)",
]

[tool.black]
line-length = 88
target-version = ["py311"]

[tool.isort]
profile = "black"
line_length = 88
```

`llmbridge` is a bare name because its repo has no remote or tags yet. It must be installed from the local checkout first (see Task 10's README step):

```bash
pip install -e ../AIInterface
pip install -e ".[dev]"
```

`src/price_monitor/__init__.py`:

```python
"""PriceMonitor: track item prices from a Google Sheet using a search-grounded LLM."""
```

- [ ] **Step 2: Write the failing test**

`tests/unit/test_models.py`:

```python
from datetime import datetime

from price_monitor.models import Item, PriceReading, PriceStatus


def test_bare_domain_is_not_a_direct_url():
    assert Item(name="Widget", website="amazon.co.uk").is_direct_url is False


def test_scheme_only_is_not_a_direct_url():
    assert Item(name="Widget", website="https://amazon.co.uk").is_direct_url is False


def test_trailing_slash_is_not_a_direct_url():
    assert Item(name="Widget", website="https://amazon.co.uk/").is_direct_url is False


def test_www_prefix_is_not_a_direct_url():
    assert Item(name="Widget", website="www.amazon.co.uk").is_direct_url is False


def test_product_path_is_a_direct_url():
    item = Item(name="Widget", website="https://www.amazon.co.uk/dp/B09XS7JWHH")
    assert item.is_direct_url is True


def test_schemeless_product_path_is_a_direct_url():
    assert Item(name="Widget", website="amazon.co.uk/dp/B09").is_direct_url is True


def test_status_serialises_as_its_string_value():
    # Compare against .value and by equality only. Enum.__format__ and __str__
    # for mixin enums changed between 3.11 and 3.12, so asserting on f-string
    # output would make this test Python-version dependent.
    assert PriceStatus.OK == "ok"
    assert PriceStatus.NOT_FOUND.value == "not_found"
    assert PriceStatus.SUSPECT.value == "suspect"


def test_reading_defaults_leave_source_and_note_blank():
    reading = PriceReading(
        timestamp=datetime(2026, 8, 20, 6, 0, 0),
        item="Widget",
        website="amazon.co.uk",
        price=279.0,
        currency="GBP",
        status=PriceStatus.OK,
    )
    assert reading.source_url == ""
    assert reading.note == ""
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/unit/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'price_monitor.models'`

- [ ] **Step 4: Write the implementation**

`src/price_monitor/models.py`:

```python
"""Value objects for PriceMonitor.

Defines the tracked :class:`Item`, the :class:`PriceReading` recorded for it on
each run, and the :class:`PriceStatus` describing how that reading turned out.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class PriceStatus(str, Enum):
    """Outcome of a single price lookup."""

    OK = "ok"
    SUSPECT = "suspect"
    NOT_FOUND = "not_found"
    WRONG_CURRENCY = "wrong_currency"
    REJECTED = "rejected"
    PARSE_ERROR = "parse_error"
    ERROR = "error"


@dataclass(frozen=True)
class Item:
    """One tracked product, as written on the Items tab."""

    name: str
    website: str

    @property
    def is_direct_url(self) -> bool:
        """True when ``website`` names a product page rather than a site to search."""
        target = self.website.strip()
        for scheme in ("https://", "http://"):
            if target.lower().startswith(scheme):
                target = target[len(scheme) :]
                break
        _, _, remainder = target.partition("/")
        return bool(remainder.strip())


@dataclass(frozen=True)
class PriceReading:
    """A single observation of an item's price at a point in time."""

    timestamp: datetime
    item: str
    website: str
    price: float | None
    currency: str
    status: PriceStatus
    source_url: str = ""
    note: str = ""
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_models.py -v`
Expected: 8 passed

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/price_monitor tests
git commit -m "feat: add project scaffolding and value objects"
```

---

### Task 2: Configuration schema

**Files:**
- Create: `src/price_monitor/app_config.py`
- Test: `tests/unit/test_app_config.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `LLMConfig`, `PriceCtrl`, `Config(drive_config: DriveConfig, llm_config: LLMConfig, price_ctrl: PriceCtrl)`, `load_price_config(path: Path) -> Config`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_app_config.py`:

```python
import json
from pathlib import Path

from price_monitor.app_config import load_price_config


def _write_config(tmp_path: Path, price_ctrl: dict | None = None) -> Path:
    payload = {
        "config": {
            "drive_config": {
                "service_file": "service_account.json",
                "remote_file": "PriceMonitor",
            },
            "llm_config": {"api_key": "test-key"},
        }
    }
    if price_ctrl is not None:
        payload["config"]["price_ctrl"] = price_ctrl
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload))
    return path


def test_missing_price_ctrl_section_falls_back_to_defaults(tmp_path):
    config = load_price_config(_write_config(tmp_path))
    assert config.price_ctrl.refresh_rate_h == 6.0
    assert config.price_ctrl.poll_rate_m == 5.0
    assert config.price_ctrl.items_sheet == "Items"
    assert config.price_ctrl.history_sheet == "Prices"
    assert config.price_ctrl.currency == "GBP"


def test_llm_defaults_target_grounded_flash(tmp_path):
    config = load_price_config(_write_config(tmp_path))
    assert config.llm_config.provider == "gemini_search"
    assert config.llm_config.model == "gemini-3.7-flash"
    assert config.llm_config.temperature == 0.0
    assert config.llm_config.api_key == "test-key"


def test_price_ctrl_values_override_defaults(tmp_path):
    path = _write_config(tmp_path, {"refresh_rate_h": 12, "currency": "EUR"})
    config = load_price_config(path)
    assert config.price_ctrl.refresh_rate_h == 12
    assert config.price_ctrl.currency == "EUR"
    assert config.price_ctrl.poll_rate_m == 5.0


def test_drive_config_is_populated(tmp_path):
    config = load_price_config(_write_config(tmp_path))
    assert config.drive_config.remote_file == "PriceMonitor"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_app_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'price_monitor.app_config'`

- [ ] **Step 3: Write the implementation**

`src/price_monitor/app_config.py`:

```python
"""Configuration schema for PriceMonitor.

Defines the dataclasses passed as ``schema`` to ``py_utils.config.load_config``
so that ``config.json`` gets proper types and automatic decryption of the API
key. Every field outside ``drive_config`` carries a default, so a config file
missing a section still loads.
"""

from dataclasses import dataclass, field
from pathlib import Path

from google_drive_api import DriveConfig
from py_utils.config import EncStr, load_config


@dataclass
class LLMConfig:
    """Credentials and model settings shared by both calls of a lookup.

    ``provider`` names the grounded search provider used for call 1. Call 2
    always uses the stock ``gemini`` provider with the same model and key.
    """

    api_key: EncStr
    provider: str = "gemini_search"
    model: str = "gemini-3.7-flash"
    max_tokens: int = 1024
    temperature: float = 0.0
    timeout: float = 60.0


@dataclass
class PriceCtrl:
    """Timing, sheet names, and validation thresholds."""

    refresh_rate_h: float = 6.0
    poll_rate_m: float = 5.0
    items_sheet: str = "Items"
    history_sheet: str = "Prices"
    currency: str = "GBP"
    suspect_threshold: float = 0.5
    max_plausible_price: float = 100000.0
    request_delay_s: float = 2.0


@dataclass
class Config:
    """Top-level ``config`` section of config.json."""

    drive_config: DriveConfig
    llm_config: LLMConfig
    price_ctrl: PriceCtrl = field(default_factory=PriceCtrl)


def load_price_config(path: Path) -> Config:
    """Load and validate config.json into a :class:`Config`."""
    return load_config(path, key="config", schema=Config)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_app_config.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/price_monitor/app_config.py tests/unit/test_app_config.py
git commit -m "feat: add configuration schema"
```

---

### Task 3: Gemini search provider

**Files:**
- Create: `src/price_monitor/search/__init__.py`
- Create: `src/price_monitor/search/gemini_search.py`
- Test: `tests/unit/test_gemini_search.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `GeminiSearchProvider` (registered as `"gemini_search"`), `grounding_urls(raw: dict) -> list[str]`

**Background — why `parse_response` is reimplemented rather than delegated:** `llmbridge`'s `GeminiProvider.parse_response` reads `candidate["content"]["parts"][0]["text"]`. With grounding on, `parts` can hold several entries and some carry no `"text"` key at all, so calling `super()` would either raise `KeyError` or silently truncate the answer to its first fragment.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_gemini_search.py`:

```python
import pytest
from llmbridge.providers import ProviderRegistry

from price_monitor.search.gemini_search import GeminiSearchProvider, grounding_urls


@pytest.fixture
def provider():
    return GeminiSearchProvider("key", "gemini-3.7-flash", 1024, 0.0)


def test_search_tool_is_injected(provider):
    body = provider.build_request_body("find a price", None, None, None)
    assert body["tools"] == [{"google_search": {}}]


def test_base_body_is_preserved(provider):
    body = provider.build_request_body("find a price", "be terse", None, None)
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "find a price"}]}
    ]
    assert body["systemInstruction"] == {"parts": [{"text": "be terse"}]}


def test_multiple_text_parts_are_joined(provider):
    raw = {
        "candidates": [
            {"content": {"parts": [{"text": "The price "}, {"text": "is 279."}]}}
        ],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4},
    }
    response = provider.parse_response(raw, 12.5)
    assert response.text == "The price is 279."
    assert response.total_tokens == 14


def test_non_text_parts_are_skipped(provider):
    raw = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"functionCall": {"name": "google_search"}},
                        {"text": "Price is 279."},
                    ]
                }
            }
        ]
    }
    assert provider.parse_response(raw, 1.0).text == "Price is 279."


def test_empty_candidates_yield_empty_text(provider):
    assert provider.parse_response({"candidates": []}, 1.0).text == ""


def test_grounding_urls_are_extracted_in_order():
    raw = {
        "candidates": [
            {
                "groundingMetadata": {
                    "groundingChunks": [
                        {"web": {"uri": "https://a.example/p", "title": "A"}},
                        {"web": {"uri": "https://b.example/p", "title": "B"}},
                    ]
                }
            }
        ]
    }
    assert grounding_urls(raw) == ["https://a.example/p", "https://b.example/p"]


def test_grounding_urls_tolerate_missing_metadata():
    assert grounding_urls({"candidates": [{}]}) == []
    assert grounding_urls({}) == []


def test_provider_is_registered():
    assert ProviderRegistry.get("gemini_search") is GeminiSearchProvider
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_gemini_search.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'price_monitor.search.gemini_search'`

- [ ] **Step 3: Write the implementation**

`src/price_monitor/search/__init__.py`:

```python
"""Price lookup via a search-grounded LLM."""
```

`src/price_monitor/search/gemini_search.py`:

```python
"""Gemini provider variant that enables Google Search grounding.

Registers ``gemini_search`` with llmbridge's :class:`ProviderRegistry` for call
one of a lookup. Call two uses the stock ``gemini`` provider unchanged, so no
second subclass is needed. Also exposes :func:`grounding_urls` for reading the
cited sources back out of a raw grounded response.
"""

from typing import Any

from llmbridge.models import PromptResponse
from llmbridge.providers import GeminiProvider, ProviderRegistry

# Verified against ai.google.dev on 2026-08-20: this is the tool key for the
# v1beta generateContent endpoint. Older models used google_search_retrieval.
_SEARCH_TOOL: list[dict[str, Any]] = [{"google_search": {}}]


class GeminiSearchProvider(GeminiProvider):
    """Gemini adapter with Google Search grounding enabled."""

    PROVIDER_NAME = "gemini_search"

    def build_request_body(
        self,
        prompt: str,
        system_prompt: str | None,
        override_max_tokens: int | None,
        override_temperature: float | None,
    ) -> dict[str, Any]:
        body = super().build_request_body(
            prompt, system_prompt, override_max_tokens, override_temperature
        )
        body["tools"] = _SEARCH_TOOL
        return body

    def parse_response(self, raw: dict[str, Any], latency_ms: float) -> PromptResponse:
        # Not delegated to super(): the base implementation indexes parts[0]
        # only, and grounding both splits the answer across parts and mixes in
        # parts that carry no "text" key at all.
        candidates = raw.get("candidates") or []
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        text = "".join(
            part["text"]
            for part in parts
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
        usage = raw.get("usageMetadata", {})
        prompt_tokens = usage.get("promptTokenCount", 0)
        completion_tokens = usage.get("candidatesTokenCount", 0)
        return PromptResponse(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            model=self.model,
            provider=self.PROVIDER_NAME,
            latency_ms=latency_ms,
            raw_response=raw,
        )


def grounding_urls(raw: dict[str, Any]) -> list[str]:
    """Return the source URIs Gemini cited for a grounded response, in order."""
    candidates = raw.get("candidates") or []
    if not candidates:
        return []
    chunks = candidates[0].get("groundingMetadata", {}).get("groundingChunks", [])
    return [
        uri
        for chunk in chunks
        if (uri := chunk.get("web", {}).get("uri", ""))
    ]


ProviderRegistry.register("gemini_search", GeminiSearchProvider)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_gemini_search.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/price_monitor/search tests/unit/test_gemini_search.py
git commit -m "feat: add Gemini search-grounded provider"
```

---

### Task 4: JSON extraction

**Files:**
- Create: `src/price_monitor/search/json_extract.py`
- Test: `tests/unit/test_json_extract.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `extract_json(text: str) -> dict` — raises `ValueError` when no usable object is present

**Background:** This is the highest-risk function in the project. Gemini has a documented failure where the leading segment of a response is dropped, so the opening `{` never arrives. The repair branch exists solely for that case.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_json_extract.py`:

```python
import pytest

from price_monitor.search.json_extract import extract_json


def test_plain_object():
    assert extract_json('{"price": 279.0}') == {"price": 279.0}


def test_markdown_fenced_object():
    text = '```json\n{"price": 279.0, "currency": "GBP"}\n```'
    assert extract_json(text) == {"price": 279.0, "currency": "GBP"}


def test_unlabelled_fence():
    assert extract_json('```\n{"price": 1.0}\n```') == {"price": 1.0}


def test_object_wrapped_in_prose():
    text = 'Here you go:\n{"price": 279.0}\nHope that helps!'
    assert extract_json(text) == {"price": 279.0}


def test_nested_braces_are_balanced():
    text = '{"price": 279.0, "meta": {"seen": {"deep": true}}} trailing'
    assert extract_json(text)["meta"]["seen"]["deep"] is True


def test_brace_inside_string_does_not_end_the_object():
    text = '{"note": "closes } here", "price": 5.0}'
    assert extract_json(text)["price"] == 5.0


def test_escaped_quote_inside_string():
    text = '{"note": "say \\"hi\\"", "price": 5.0}'
    assert extract_json(text)["note"] == 'say "hi"'


def test_leading_truncation_is_repaired():
    # The documented Gemini grounding failure: the opening brace is dropped.
    text = '"price": 279.0, "currency": "GBP"}'
    assert extract_json(text) == {"price": 279.0, "currency": "GBP"}


def test_empty_reply_raises():
    with pytest.raises(ValueError):
        extract_json("")


def test_whitespace_only_reply_raises():
    with pytest.raises(ValueError):
        extract_json("   \n  ")


def test_no_object_raises():
    with pytest.raises(ValueError):
        extract_json("I could not find a price anywhere.")


def test_unterminated_object_raises():
    with pytest.raises(ValueError):
        extract_json('{"price": 279.0')


def test_malformed_json_raises():
    with pytest.raises(ValueError):
        extract_json('{"price": 279.0,,}')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_json_extract.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'price_monitor.search.json_extract'`

- [ ] **Step 3: Write the implementation**

`src/price_monitor/search/json_extract.py`:

```python
"""Tolerant extraction of a single JSON object from an LLM reply.

Model replies wrap JSON in markdown fences or prose, and Gemini's grounded
responses can lose their leading segment entirely. :func:`extract_json` copes
with all of these and raises :class:`ValueError` rather than letting a bad
reply escape as an exception type the caller does not expect.
"""

import json
from typing import Any


def extract_json(text: str) -> dict[str, Any]:
    """Return the first JSON object in ``text``.

    Raises:
        ValueError: when the reply is empty, holds no object, or the object
            found is not valid JSON.
    """
    if not text or not text.strip():
        raise ValueError("empty reply")

    cleaned = _strip_fences(text)
    candidate = _first_balanced_object(cleaned)

    # Repairs the documented Gemini grounding failure in which the leading
    # segment of the reply is dropped, taking the opening brace with it.
    if candidate is None and "}" in cleaned and "{" not in cleaned:
        candidate = _first_balanced_object("{" + cleaned)

    if candidate is None:
        raise ValueError("no JSON object found in reply")

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON in reply: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("reply JSON was not an object")
    return parsed


def _strip_fences(text: str) -> str:
    """Remove a surrounding markdown code fence, if present."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _first_balanced_object(text: str) -> str | None:
    """Return the first brace-balanced ``{...}`` span, ignoring braces in strings."""
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_json_extract.py -v`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add src/price_monitor/search/json_extract.py tests/unit/test_json_extract.py
git commit -m "feat: add tolerant JSON extraction"
```

---

### Task 5: Prompt templates

**Files:**
- Create: `src/price_monitor/search/prompts.py`
- Test: `tests/unit/test_prompts.py`

**Interfaces:**
- Consumes: `Item` (Task 1)
- Produces: `search_prompt(item: Item) -> str`, `format_prompt(report: str, urls: list[str]) -> str`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_prompts.py`:

```python
from price_monitor.models import Item
from price_monitor.search.prompts import format_prompt, search_prompt


def test_site_prompt_used_for_bare_domain():
    prompt = search_prompt(Item(name="Sony WH-1000XM5", website="amazon.co.uk"))
    assert "Sony WH-1000XM5" in prompt
    assert "amazon.co.uk" in prompt
    assert "Search that site" in prompt


def test_page_prompt_used_for_direct_url():
    item = Item(name="Sony WH-1000XM5", website="https://amazon.co.uk/dp/B09")
    prompt = search_prompt(item)
    assert "Read that page" in prompt
    assert "https://amazon.co.uk/dp/B09" in prompt


def test_both_prompts_forbid_guessing():
    site = search_prompt(Item(name="X", website="a.com"))
    page = search_prompt(Item(name="X", website="a.com/p"))
    assert "Do not guess" in site
    assert "Do not guess" in page


def test_format_prompt_embeds_report_and_urls():
    prompt = format_prompt("It costs 279 GBP.", ["https://a.example/p"])
    assert "It costs 279 GBP." in prompt
    assert "https://a.example/p" in prompt


def test_format_prompt_handles_no_urls():
    assert "none" in format_prompt("It costs 279 GBP.", [])


def test_format_prompt_schema_braces_survive_formatting():
    # The schema block must reach the model as literal JSON, not be consumed
    # by str.format placeholders.
    prompt = format_prompt("report", [])
    assert '"price"' in prompt
    assert '"in_stock"' in prompt
    assert "{" in prompt and "}" in prompt


def test_format_prompt_braces_are_balanced():
    prompt = format_prompt("report", [])
    assert prompt.count("{") == prompt.count("}")


def test_format_prompt_names_every_schema_field():
    prompt = format_prompt("report", [])
    for field in ("price", "currency", "url", "in_stock", "found", "note"):
        assert f'"{field}"' in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_prompts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'price_monitor.search.prompts'`

- [ ] **Step 3: Write the implementation**

`src/price_monitor/search/prompts.py`:

```python
"""Prompt templates for the two-call price lookup.

Call one asks a grounded model for a prose report; call two converts that
report into JSON with no grounding in play. Keeping the templates here makes
them reviewable without reading the search plumbing.
"""

from price_monitor.models import Item

_SITE_TEMPLATE = """Find the current price of this product.

Product: {item}
Site to search: {website}

Search that site and report:
- the price shown right now, with its currency
- the exact URL of the page you read the price from
- whether it is in stock

If you cannot find the product on that site, say so plainly.
Do not guess a price. Report only a price you actually saw."""

_PAGE_TEMPLATE = """Find the current price of the product on this page.

Product: {item}
Page: {website}

Read that page and report:
- the price shown right now, with its currency
- the exact URL you read the price from
- whether it is in stock

If the page does not show a price, say so plainly.
Do not guess a price. Report only a price you actually saw."""

_FORMAT_TEMPLATE = """Convert the report below into one JSON object and nothing else.

Schema:
{{"price": <number or null>, "currency": "<ISO 4217 code>", "url": "<string>", "in_stock": <true or false>, "found": <true or false>, "note": "<string>"}}

Rules:
- Set found to false when the report gives no price; price is then null.
- price is a plain number: no currency symbol, no thousands separators.
- url is the page the price was read from. Use one of the known source URLs
  below if the report does not name a page.
- note explains why found is false; otherwise use an empty string.
- Output the JSON object only. No markdown fences, no commentary.

Known source URLs: {urls}

Report:
{report}"""


def search_prompt(item: Item) -> str:
    """Build the call-one prompt, choosing site-search or single-page wording."""
    template = _PAGE_TEMPLATE if item.is_direct_url else _SITE_TEMPLATE
    return template.format(item=item.name, website=item.website)


def format_prompt(report: str, urls: list[str]) -> str:
    """Build the call-two prompt that turns a prose report into JSON."""
    return _FORMAT_TEMPLATE.format(urls=", ".join(urls) if urls else "none", report=report)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_prompts.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/price_monitor/search/prompts.py tests/unit/test_prompts.py
git commit -m "feat: add price lookup prompt templates"
```

---

### Task 6: Validation ladder

**Files:**
- Create: `src/price_monitor/search/validation.py`
- Test: `tests/unit/test_validation.py`

**Interfaces:**
- Consumes: `Item`, `PriceReading`, `PriceStatus` (Task 1); `PriceCtrl` (Task 2)
- Produces: `validate(payload: dict, item: Item, timestamp: datetime, ctrl: PriceCtrl, last_price: float | None, fallback_urls: list[str]) -> PriceReading`

**Ladder order (first match wins):** `found` false → `NOT_FOUND`; currency mismatch → `WRONG_CURRENCY`; non-numeric, `<= 0`, or above `max_plausible_price` → `REJECTED`; relative move beyond `suspect_threshold` → `SUSPECT` (price still recorded); otherwise `OK`.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_validation.py`:

```python
from datetime import datetime

from price_monitor.app_config import PriceCtrl
from price_monitor.models import Item, PriceStatus
from price_monitor.search.validation import validate

NOW = datetime(2026, 8, 20, 6, 0, 0)
ITEM = Item(name="Widget", website="shop.example")
CTRL = PriceCtrl()


def _payload(**overrides):
    payload = {
        "price": 100.0,
        "currency": "GBP",
        "url": "https://shop.example/widget",
        "in_stock": True,
        "found": True,
        "note": "",
    }
    payload.update(overrides)
    return payload


def _validate(payload, last_price=None, urls=None):
    return validate(payload, ITEM, NOW, CTRL, last_price, urls or [])


def test_clean_reading_is_ok():
    reading = _validate(_payload())
    assert reading.status == PriceStatus.OK
    assert reading.price == 100.0
    assert reading.source_url == "https://shop.example/widget"
    assert reading.item == "Widget"
    assert reading.website == "shop.example"


def test_not_found_blanks_the_price_and_keeps_the_note():
    reading = _validate(_payload(found=False, price=None, note="discontinued"))
    assert reading.status == PriceStatus.NOT_FOUND
    assert reading.price is None
    assert reading.note == "discontinued"


def test_currency_mismatch_blanks_the_price():
    reading = _validate(_payload(currency="USD"))
    assert reading.status == PriceStatus.WRONG_CURRENCY
    assert reading.price is None


def test_currency_comparison_ignores_case_and_padding():
    assert _validate(_payload(currency=" gbp ")).status == PriceStatus.OK


def test_zero_price_is_rejected():
    reading = _validate(_payload(price=0))
    assert reading.status == PriceStatus.REJECTED
    assert reading.price is None


def test_negative_price_is_rejected():
    assert _validate(_payload(price=-5)).status == PriceStatus.REJECTED


def test_absurd_price_is_rejected():
    assert _validate(_payload(price=999_999_999)).status == PriceStatus.REJECTED


def test_non_numeric_price_is_rejected():
    assert _validate(_payload(price="two hundred")).status == PriceStatus.REJECTED


def test_numeric_string_price_is_accepted():
    reading = _validate(_payload(price="279.00"))
    assert reading.status == PriceStatus.OK
    assert reading.price == 279.0


def test_large_move_is_suspect_but_still_recorded():
    reading = _validate(_payload(price=300.0), last_price=100.0)
    assert reading.status == PriceStatus.SUSPECT
    assert reading.price == 300.0


def test_small_move_is_ok():
    assert _validate(_payload(price=110.0), last_price=100.0).status == PriceStatus.OK


def test_first_reading_is_never_suspect():
    assert _validate(_payload(price=100.0), last_price=None).status == PriceStatus.OK


def test_source_url_falls_back_to_grounding_when_blank():
    reading = _validate(_payload(url=""), urls=["https://grounded.example/p"])
    assert reading.source_url == "https://grounded.example/p"


def test_source_url_prefers_the_reported_url():
    reading = _validate(_payload(), urls=["https://grounded.example/p"])
    assert reading.source_url == "https://shop.example/widget"


def test_source_url_blank_when_nothing_available():
    assert _validate(_payload(url=""), urls=[]).source_url == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_validation.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'price_monitor.search.validation'`

- [ ] **Step 3: Write the implementation**

`src/price_monitor/search/validation.py`:

```python
"""Turn a parsed model payload into a validated :class:`PriceReading`.

Applies the plausibility ladder that keeps hallucinated or mis-matched prices
out of the recorded history, or flags them where they are merely suspicious.
"""

from datetime import datetime

from price_monitor.app_config import PriceCtrl
from price_monitor.models import Item, PriceReading, PriceStatus


def validate(
    payload: dict,
    item: Item,
    timestamp: datetime,
    ctrl: PriceCtrl,
    last_price: float | None,
    fallback_urls: list[str],
) -> PriceReading:
    """Grade ``payload`` against the plausibility ladder."""
    source_url = str(payload.get("url") or "").strip()
    if not source_url and fallback_urls:
        source_url = fallback_urls[0]
    note = str(payload.get("note") or "").strip()

    def reading(
        price: float | None, status: PriceStatus, text: str = ""
    ) -> PriceReading:
        return PriceReading(
            timestamp=timestamp,
            item=item.name,
            website=item.website,
            price=price,
            currency=ctrl.currency,
            status=status,
            source_url=source_url,
            note=text or note,
        )

    if not payload.get("found", False):
        return reading(None, PriceStatus.NOT_FOUND)

    currency = str(payload.get("currency") or "").strip().upper()
    if currency != ctrl.currency.strip().upper():
        return reading(None, PriceStatus.WRONG_CURRENCY, f"reported {currency or '?'}")

    try:
        price = float(payload.get("price"))
    except (TypeError, ValueError):
        return reading(None, PriceStatus.REJECTED, "price was not a number")

    if price <= 0 or price > ctrl.max_plausible_price:
        return reading(None, PriceStatus.REJECTED, f"implausible price {price}")

    if last_price is not None and last_price > 0:
        move = abs(price - last_price) / last_price
        if move > ctrl.suspect_threshold:
            return reading(
                price, PriceStatus.SUSPECT, f"moved {move:.0%} from {last_price}"
            )

    return reading(price, PriceStatus.OK)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_validation.py -v`
Expected: 15 passed

- [ ] **Step 5: Commit**

```bash
git add src/price_monitor/search/validation.py tests/unit/test_validation.py
git commit -m "feat: add price validation ladder"
```

---

### Task 7: PriceSearcher two-call orchestration

**Files:**
- Create: `src/price_monitor/search/searcher.py`
- Test: `tests/unit/test_searcher.py`

**Interfaces:**
- Consumes: `Item`, `PriceReading`, `PriceStatus` (T1); `LLMConfig`, `PriceCtrl` (T2); `grounding_urls` (T3); `extract_json` (T4); `search_prompt`, `format_prompt` (T5); `validate` (T6)
- Produces: `PriceSearcher(config: LLMConfig, ctrl: PriceCtrl, logger: logging.Logger, search_client=None, format_client=None)` with `price(item: Item, last_price: float | None, timestamp: datetime) -> PriceReading`

**Sequence:** call 1 (grounded, prose) → short-circuit to `ERROR`/`PARSE_ERROR` on failure without making call 2 → call 2 (un-grounded, JSON) → `extract_json` → `validate`.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_searcher.py`:

```python
import json
import logging
from datetime import datetime

import pytest
from llmbridge.exceptions import LLMConnectionError, LLMRateLimitError
from llmbridge.models import PromptResponse

from price_monitor.app_config import LLMConfig, PriceCtrl
from price_monitor.models import Item, PriceStatus
from price_monitor.search.searcher import PriceSearcher

NOW = datetime(2026, 8, 20, 6, 0, 0)
ITEM = Item(name="Widget", website="shop.example")


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


def _searcher(search_client, format_client, logger, ctrl=None):
    return PriceSearcher(
        config=LLMConfig(api_key="k"),
        ctrl=ctrl or PriceCtrl(),
        logger=logger,
        search_client=search_client,
        format_client=format_client,
    )


_GOOD_JSON = json.dumps(
    {
        "price": 279.0,
        "currency": "GBP",
        "url": "https://shop.example/w",
        "in_stock": True,
        "found": True,
        "note": "",
    }
)


def test_happy_path_returns_an_ok_reading(logger):
    search = FakeClient(_response("It costs 279 GBP."))
    fmt = FakeClient(_response(_GOOD_JSON))
    reading = _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert reading.status == PriceStatus.OK
    assert reading.price == 279.0
    assert reading.timestamp == NOW


def test_call_one_prose_is_passed_into_call_two(logger):
    search = FakeClient(_response("It costs 279 GBP at shop.example."))
    fmt = FakeClient(_response(_GOOD_JSON))
    _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert "It costs 279 GBP at shop.example." in fmt.prompts[0]


def test_grounding_urls_reach_call_two(logger):
    raw = {
        "candidates": [
            {
                "groundingMetadata": {
                    "groundingChunks": [{"web": {"uri": "https://g.example/p"}}]
                }
            }
        ]
    }
    search = FakeClient(_response("It costs 279 GBP.", raw))
    fmt = FakeClient(_response(_GOOD_JSON))
    _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert "https://g.example/p" in fmt.prompts[0]


def test_call_one_failure_short_circuits_call_two(logger):
    search = FakeClient(LLMConnectionError("boom"))
    fmt = FakeClient(_response(_GOOD_JSON))
    reading = _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert reading.status == PriceStatus.ERROR
    assert reading.price is None
    assert fmt.prompts == []


def test_rate_limit_on_call_two_yields_error(logger):
    search = FakeClient(_response("It costs 279 GBP."))
    fmt = FakeClient(LLMRateLimitError("slow down"))
    reading = _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert reading.status == PriceStatus.ERROR


def test_empty_call_one_reply_is_a_parse_error(logger):
    search = FakeClient(_response("   "))
    fmt = FakeClient(_response(_GOOD_JSON))
    reading = _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert reading.status == PriceStatus.PARSE_ERROR
    assert fmt.prompts == []


def test_unparseable_call_two_reply_is_a_parse_error(logger):
    search = FakeClient(_response("It costs 279 GBP."))
    fmt = FakeClient(_response("I am afraid I cannot do that."))
    reading = _searcher(search, fmt, logger).price(ITEM, None, NOW)
    assert reading.status == PriceStatus.PARSE_ERROR


def test_last_price_drives_the_suspect_check(logger):
    search = FakeClient(_response("It costs 279 GBP."))
    fmt = FakeClient(_response(_GOOD_JSON))
    reading = _searcher(search, fmt, logger).price(ITEM, 50.0, NOW)
    assert reading.status == PriceStatus.SUSPECT


def test_direct_url_item_uses_the_page_prompt(logger):
    item = Item(name="Widget", website="https://shop.example/dp/1")
    search = FakeClient(_response("It costs 279 GBP."))
    fmt = FakeClient(_response(_GOOD_JSON))
    _searcher(search, fmt, logger).price(item, None, NOW)
    assert "Read that page" in search.prompts[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_searcher.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'price_monitor.search.searcher'`

- [ ] **Step 3: Write the implementation**

`src/price_monitor/search/searcher.py`:

```python
"""Two-call price lookup.

Call one asks a search-grounded model for a prose report; call two converts
that report to JSON with grounding switched off, because Gemini's grounding and
structured output are unreliable together. Every failure path returns a
:class:`PriceReading` carrying a status — nothing raises out of :meth:`price`.
"""

import logging
from datetime import datetime

from llmbridge import LLMClient
from llmbridge.exceptions import LLMError
from py_utils.logger import get_child_logger

from price_monitor.app_config import LLMConfig, PriceCtrl
from price_monitor.models import Item, PriceReading, PriceStatus
from price_monitor.search import gemini_search  # noqa: F401  (registers the provider)
from price_monitor.search.gemini_search import grounding_urls
from price_monitor.search.json_extract import extract_json
from price_monitor.search.prompts import format_prompt, search_prompt
from price_monitor.search.validation import validate

# Call two must not be grounded: it is the un-grounded regime that makes JSON
# output reliable, which is the whole reason the lookup is split in two.
_FORMAT_PROVIDER = "gemini"


class PriceSearcher:
    """Prices a single :class:`Item` using a grounded search then a formatter."""

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
        self._search_client = search_client or LLMClient(
            provider=config.provider,
            api_key=config.api_key,
            model=config.model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.timeout,
        )
        self._format_client = format_client or LLMClient(
            provider=_FORMAT_PROVIDER,
            api_key=config.api_key,
            model=config.model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.timeout,
        )

    def price(
        self, item: Item, last_price: float | None, timestamp: datetime
    ) -> PriceReading:
        """Look up ``item``'s current price, never raising on failure."""
        try:
            searched = self._search_client.prompt(search_prompt(item))
        except LLMError as exc:
            self.logger.warning(f"Search call failed for '{item.name}': {exc}")
            return self._failed(item, timestamp, PriceStatus.ERROR, str(exc))

        if not searched.text or not searched.text.strip():
            self.logger.warning(f"Search returned no text for '{item.name}'")
            return self._failed(
                item, timestamp, PriceStatus.PARSE_ERROR, "empty search reply"
            )

        urls = grounding_urls(searched.raw_response)

        try:
            formatted = self._format_client.prompt(format_prompt(searched.text, urls))
        except LLMError as exc:
            self.logger.warning(f"Format call failed for '{item.name}': {exc}")
            return self._failed(item, timestamp, PriceStatus.ERROR, str(exc))

        try:
            payload = extract_json(formatted.text)
        except ValueError as exc:
            self.logger.warning(f"Unparseable reply for '{item.name}': {exc}")
            return self._failed(item, timestamp, PriceStatus.PARSE_ERROR, str(exc))

        return validate(payload, item, timestamp, self._ctrl, last_price, urls)

    def _failed(
        self, item: Item, timestamp: datetime, status: PriceStatus, note: str
    ) -> PriceReading:
        return PriceReading(
            timestamp=timestamp,
            item=item.name,
            website=item.website,
            price=None,
            currency=self._ctrl.currency,
            status=status,
            note=note,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_searcher.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add src/price_monitor/search/searcher.py tests/unit/test_searcher.py
git commit -m "feat: add two-call price searcher"
```

---

### Task 8: History tab

**Files:**
- Create: `src/price_monitor/sheets/__init__.py`
- Create: `src/price_monitor/sheets/history_tab.py`
- Test: `tests/unit/test_history_tab.py`

**Interfaces:**
- Consumes: `PriceReading`, `PriceStatus` (T1)
- Produces: `HistoryTab(gsheet, sheet_name: str, logger)` with `read() -> pd.DataFrame`, `append(readings: list[PriceReading]) -> None`, `last_prices() -> dict[tuple[str, str], float]`, `known_items() -> set[tuple[str, str]]`, `summarise() -> pd.DataFrame`; module constant `TIMESTAMP_FORMAT`

**Background — why `update()` and not `write()`:** the tab is append-only, so no clear is needed, and `write()` clears before writing, opening a window where a crash would destroy the whole history. The one exception is the very first write, when the tab may not exist yet: `update()` does not create tabs, so an empty history uses `write()`, which does — and at that point there is no history to lose.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_history_tab.py`:

```python
import logging
from datetime import datetime

import pandas as pd
import pytest

from price_monitor.models import PriceReading, PriceStatus
from price_monitor.sheets.history_tab import HistoryTab


class FakeSheet:
    """Stands in for GoogleSheetInterface, recording which write path was used."""

    def __init__(self, frame: pd.DataFrame | None = None):
        self.frame = frame if frame is not None else pd.DataFrame()
        self.writes: list[str] = []

    def read(self, sheet_name: str) -> pd.DataFrame:
        return self.frame.copy()

    def write(self, sheet_name: str, df: pd.DataFrame) -> None:
        self.writes.append("write")
        self.frame = df.copy()

    def update(self, sheet_name: str, df: pd.DataFrame) -> None:
        self.writes.append("update")
        self.frame = df.copy()


@pytest.fixture
def logger():
    return logging.getLogger("test")


def _reading(item="Widget", price=100.0, when="2026-08-20 06:00:00", status=PriceStatus.OK):
    return PriceReading(
        timestamp=datetime.strptime(when, "%Y-%m-%d %H:%M:%S"),
        item=item,
        website="shop.example",
        price=price,
        currency="GBP",
        status=status,
        source_url="https://shop.example/w",
    )


def _history(rows: list[dict]) -> pd.DataFrame:
    columns = [
        "timestamp", "item", "website", "price",
        "currency", "status", "source_url", "note",
    ]
    return pd.DataFrame(rows, columns=columns)


def test_first_append_uses_write_so_the_tab_is_created(logger):
    sheet = FakeSheet()
    HistoryTab(sheet, "Prices", logger).append([_reading()])
    assert sheet.writes == ["write"]
    assert len(sheet.frame) == 1


def test_subsequent_append_uses_update_to_protect_history(logger):
    existing = _history([{
        "timestamp": "2026-08-20 06:00:00", "item": "Widget",
        "website": "shop.example", "price": "100.0", "currency": "GBP",
        "status": "ok", "source_url": "", "note": "",
    }])
    sheet = FakeSheet(existing)
    HistoryTab(sheet, "Prices", logger).append([_reading(when="2026-08-20 12:00:00")])
    assert sheet.writes == ["update"]
    assert len(sheet.frame) == 2


def test_append_of_nothing_writes_nothing(logger):
    sheet = FakeSheet()
    HistoryTab(sheet, "Prices", logger).append([])
    assert sheet.writes == []


def test_timestamp_is_written_in_the_agreed_format(logger):
    sheet = FakeSheet()
    HistoryTab(sheet, "Prices", logger).append([_reading()])
    assert sheet.frame.iloc[0]["timestamp"] == "2026-08-20 06:00:00"


def test_status_is_written_as_its_string_value(logger):
    sheet = FakeSheet()
    HistoryTab(sheet, "Prices", logger).append([_reading(status=PriceStatus.SUSPECT)])
    assert sheet.frame.iloc[0]["status"] == "suspect"


def test_null_price_is_written_blank(logger):
    sheet = FakeSheet()
    HistoryTab(sheet, "Prices", logger).append(
        [_reading(price=None, status=PriceStatus.NOT_FOUND)]
    )
    assert sheet.frame.iloc[0]["price"] == ""


def test_last_prices_uses_the_most_recent_non_null(logger):
    frame = _history([
        {"timestamp": "2026-08-20 06:00:00", "item": "Widget", "website": "shop.example",
         "price": "100.0", "currency": "GBP", "status": "ok", "source_url": "", "note": ""},
        {"timestamp": "2026-08-20 12:00:00", "item": "Widget", "website": "shop.example",
         "price": "120.0", "currency": "GBP", "status": "ok", "source_url": "", "note": ""},
        {"timestamp": "2026-08-20 18:00:00", "item": "Widget", "website": "shop.example",
         "price": "", "currency": "GBP", "status": "not_found", "source_url": "", "note": ""},
    ])
    tab = HistoryTab(FakeSheet(frame), "Prices", logger)
    assert tab.last_prices()[("Widget", "shop.example")] == 120.0


def test_known_items_includes_items_with_no_successful_price(logger):
    frame = _history([
        {"timestamp": "2026-08-20 06:00:00", "item": "Ghost", "website": "shop.example",
         "price": "", "currency": "GBP", "status": "not_found", "source_url": "", "note": ""},
    ])
    tab = HistoryTab(FakeSheet(frame), "Prices", logger)
    assert ("Ghost", "shop.example") in tab.known_items()


def test_summarise_computes_current_min_and_mean(logger):
    frame = _history([
        {"timestamp": "2026-08-20 06:00:00", "item": "Widget", "website": "shop.example",
         "price": "100.0", "currency": "GBP", "status": "ok", "source_url": "", "note": ""},
        {"timestamp": "2026-08-20 12:00:00", "item": "Widget", "website": "shop.example",
         "price": "50.0", "currency": "GBP", "status": "ok", "source_url": "", "note": ""},
        {"timestamp": "2026-08-20 18:00:00", "item": "Widget", "website": "shop.example",
         "price": "60.0", "currency": "GBP", "status": "ok", "source_url": "", "note": ""},
    ])
    row = HistoryTab(FakeSheet(frame), "Prices", logger).summarise().iloc[0]
    assert row["current"] == 60.0
    assert row["min"] == 50.0
    assert row["mean"] == 70.0
    assert row["last_checked"] == "2026-08-20 18:00:00"


def test_last_checked_reflects_failures_too(logger):
    frame = _history([
        {"timestamp": "2026-08-20 06:00:00", "item": "Widget", "website": "shop.example",
         "price": "100.0", "currency": "GBP", "status": "ok", "source_url": "", "note": ""},
        {"timestamp": "2026-08-20 12:00:00", "item": "Widget", "website": "shop.example",
         "price": "", "currency": "GBP", "status": "error", "source_url": "", "note": ""},
    ])
    row = HistoryTab(FakeSheet(frame), "Prices", logger).summarise().iloc[0]
    assert row["current"] == 100.0
    assert row["last_checked"] == "2026-08-20 12:00:00"


def test_item_with_no_prices_summarises_blank(logger):
    frame = _history([
        {"timestamp": "2026-08-20 06:00:00", "item": "Ghost", "website": "shop.example",
         "price": "", "currency": "GBP", "status": "not_found", "source_url": "", "note": ""},
    ])
    row = HistoryTab(FakeSheet(frame), "Prices", logger).summarise().iloc[0]
    assert row["current"] == ""
    assert row["min"] == ""
    assert row["mean"] == ""


def test_summarise_of_empty_history_is_empty(logger):
    assert HistoryTab(FakeSheet(), "Prices", logger).summarise().empty
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_history_tab.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'price_monitor.sheets.history_tab'`

- [ ] **Step 3: Write the implementation**

`src/price_monitor/sheets/__init__.py`:

```python
"""Google Sheets tabs used by PriceMonitor."""
```

`src/price_monitor/sheets/history_tab.py`:

```python
"""Append-only price history tab.

Owns the long-format Prices tab: one row per item per run, plus the statistics
derived from it. Sheet values arrive as strings, so prices are coerced on read
and rendered back as strings on write.
"""

import logging

import pandas as pd
from py_utils.logger import get_child_logger

from price_monitor.models import PriceReading

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

COLUMNS = [
    "timestamp",
    "item",
    "website",
    "price",
    "currency",
    "status",
    "source_url",
    "note",
]

SUMMARY_COLUMNS = ["item", "website", "current", "min", "mean", "last_checked"]


class HistoryTab:
    """Reads and appends the long-format price history."""

    def __init__(self, gsheet, sheet_name: str, logger: logging.Logger) -> None:
        self.logger = get_child_logger(logger, __class__.__name__)
        self._gsheet = gsheet
        self._sheet_name = sheet_name

    def read(self) -> pd.DataFrame:
        """Return the raw history frame, with all expected columns present."""
        frame = self._gsheet.read(sheet_name=self._sheet_name)
        if frame.empty:
            return pd.DataFrame(columns=COLUMNS)
        for column in COLUMNS:
            if column not in frame.columns:
                frame[column] = ""
        return frame[COLUMNS]

    def append(self, readings: list[PriceReading]) -> None:
        """Append readings to the tab, creating it on first use."""
        if not readings:
            return
        new_rows = pd.DataFrame([_to_row(r) for r in readings], columns=COLUMNS)
        existing = self.read()

        if existing.empty:
            # update() cannot create a missing tab; write() can, and there is no
            # history to lose on this path.
            self._gsheet.write(sheet_name=self._sheet_name, df=new_rows)
        else:
            # update() writes from A1 without clearing. The tab only ever grows,
            # so no clear is needed — and write()'s clear-then-write would risk
            # losing the entire history if the process died between the two.
            combined = pd.concat([existing, new_rows], ignore_index=True)
            self._gsheet.update(sheet_name=self._sheet_name, df=combined)

        self.logger.info(f"Appended {len(readings)} readings")

    def last_prices(self) -> dict[tuple[str, str], float]:
        """Most recent non-null price for each (item, website) pair."""
        frame = self._typed()
        prices: dict[tuple[str, str], float] = {}
        if frame.empty:
            return prices
        priced = frame[frame["price"].notna()]
        for key, group in priced.groupby(["item", "website"], sort=False):
            prices[key] = float(group.sort_values("timestamp")["price"].iloc[-1])
        return prices

    def known_items(self) -> set[tuple[str, str]]:
        """Every (item, website) pair that has ever been recorded."""
        frame = self.read()
        if frame.empty:
            return set()
        return set(zip(frame["item"], frame["website"]))

    def summarise(self) -> pd.DataFrame:
        """Per-item current, min, mean, and last-checked timestamp."""
        frame = self._typed()
        if frame.empty:
            return pd.DataFrame(columns=SUMMARY_COLUMNS)

        rows = []
        for (item, website), group in frame.groupby(["item", "website"], sort=False):
            ordered = group.sort_values("timestamp")
            priced = ordered[ordered["price"].notna()]
            rows.append(
                {
                    "item": item,
                    "website": website,
                    "current": float(priced["price"].iloc[-1]) if not priced.empty else "",
                    "min": float(priced["price"].min()) if not priced.empty else "",
                    "mean": round(float(priced["price"].mean()), 2) if not priced.empty else "",
                    # Any status, so a run of failures shows as a moving
                    # timestamp beside a stale price rather than looking healthy.
                    "last_checked": ordered["timestamp"].iloc[-1],
                }
            )
        return pd.DataFrame(rows, columns=SUMMARY_COLUMNS)

    def _typed(self) -> pd.DataFrame:
        """History with the price column coerced to numbers, blanks becoming NaN."""
        frame = self.read()
        if frame.empty:
            return frame
        frame = frame.copy()
        frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
        return frame


def _to_row(reading: PriceReading) -> dict[str, str]:
    return {
        "timestamp": reading.timestamp.strftime(TIMESTAMP_FORMAT),
        "item": reading.item,
        "website": reading.website,
        "price": "" if reading.price is None else f"{reading.price:.2f}",
        "currency": reading.currency,
        "status": reading.status.value,
        "source_url": reading.source_url,
        "note": reading.note,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_history_tab.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add src/price_monitor/sheets tests/unit/test_history_tab.py
git commit -m "feat: add append-only price history tab"
```

---

### Task 9: Items tab

**Files:**
- Create: `src/price_monitor/sheets/items_tab.py`
- Test: `tests/unit/test_items_tab.py`

**Interfaces:**
- Consumes: `Item` (T1); consumes T8's summary frame by column name (`item`, `website`, `current`, `min`, `mean`, `last_checked`), not by import
- Produces: `ItemsTab(gsheet, sheet_name: str, logger)` with `read() -> list[Item]`, `write_summary(items: list[Item], summary: pd.DataFrame) -> None`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_items_tab.py`:

```python
import logging

import pandas as pd
import pytest

from price_monitor.models import Item
from price_monitor.sheets.items_tab import ItemsTab


class FakeSheet:
    def __init__(self, frame: pd.DataFrame | None = None):
        self.frame = frame if frame is not None else pd.DataFrame()
        self.writes: list[str] = []

    def read(self, sheet_name: str) -> pd.DataFrame:
        return self.frame.copy()

    def write(self, sheet_name: str, df: pd.DataFrame) -> None:
        self.writes.append("write")
        self.frame = df.copy()

    def update(self, sheet_name: str, df: pd.DataFrame) -> None:
        self.writes.append("update")
        self.frame = df.copy()


@pytest.fixture
def logger():
    return logging.getLogger("test")


def _items_frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["item", "website"])


def test_reads_items_in_sheet_order(logger):
    frame = _items_frame([
        {"item": "Widget", "website": "shop.example"},
        {"item": "Gadget", "website": "other.example"},
    ])
    items = ItemsTab(FakeSheet(frame), "Items", logger).read()
    assert items == [
        Item(name="Widget", website="shop.example"),
        Item(name="Gadget", website="other.example"),
    ]


def test_blank_name_row_is_skipped(logger):
    frame = _items_frame([
        {"item": "", "website": "shop.example"},
        {"item": "Widget", "website": "shop.example"},
    ])
    assert ItemsTab(FakeSheet(frame), "Items", logger).read() == [
        Item(name="Widget", website="shop.example")
    ]


def test_blank_website_row_is_skipped(logger):
    frame = _items_frame([{"item": "Widget", "website": "  "}])
    assert ItemsTab(FakeSheet(frame), "Items", logger).read() == []


def test_surrounding_whitespace_is_trimmed(logger):
    frame = _items_frame([{"item": "  Widget  ", "website": " shop.example "}])
    assert ItemsTab(FakeSheet(frame), "Items", logger).read() == [
        Item(name="Widget", website="shop.example")
    ]


def test_duplicate_pairs_are_deduplicated(logger):
    frame = _items_frame([
        {"item": "Widget", "website": "shop.example"},
        {"item": "Widget", "website": "shop.example"},
    ])
    assert len(ItemsTab(FakeSheet(frame), "Items", logger).read()) == 1


def test_same_item_on_two_sites_is_kept(logger):
    frame = _items_frame([
        {"item": "Widget", "website": "shop.example"},
        {"item": "Widget", "website": "other.example"},
    ])
    assert len(ItemsTab(FakeSheet(frame), "Items", logger).read()) == 2


def test_empty_tab_yields_no_items(logger):
    assert ItemsTab(FakeSheet(), "Items", logger).read() == []


def test_tab_without_expected_headers_yields_no_items(logger):
    frame = pd.DataFrame([{"thing": "Widget"}])
    assert ItemsTab(FakeSheet(frame), "Items", logger).read() == []


def test_write_summary_uses_write_so_deletions_do_not_linger(logger):
    sheet = FakeSheet()
    summary = pd.DataFrame(
        [{"item": "Widget", "website": "shop.example", "current": 60.0,
          "min": 50.0, "mean": 70.0, "last_checked": "2026-08-20 18:00:00"}]
    )
    ItemsTab(sheet, "Items", logger).write_summary(
        [Item(name="Widget", website="shop.example")], summary
    )
    assert sheet.writes == ["write"]
    row = sheet.frame.iloc[0]
    assert row["current"] == 60.0
    assert row["mean"] == 70.0


def test_item_without_history_gets_blank_summary_cells(logger):
    sheet = FakeSheet()
    ItemsTab(sheet, "Items", logger).write_summary(
        [Item(name="New", website="shop.example")], pd.DataFrame()
    )
    row = sheet.frame.iloc[0]
    assert row["item"] == "New"
    assert row["current"] == ""
    assert row["last_checked"] == ""


def test_summary_row_order_follows_the_items_list(logger):
    sheet = FakeSheet()
    items = [
        Item(name="B", website="shop.example"),
        Item(name="A", website="shop.example"),
    ]
    ItemsTab(sheet, "Items", logger).write_summary(items, pd.DataFrame())
    assert list(sheet.frame["item"]) == ["B", "A"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_items_tab.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'price_monitor.sheets.items_tab'`

- [ ] **Step 3: Write the implementation**

`src/price_monitor/sheets/items_tab.py`:

```python
"""User-facing Items tab.

Columns A-B (``item``, ``website``) are owned by the user; the summary columns
after them are rewritten on every run. Malformed rows are skipped rather than
allowed to reach the LLM.
"""

import logging

import pandas as pd
from py_utils.logger import get_child_logger

from price_monitor.models import Item

COLUMNS = ["item", "website", "current", "min", "mean", "last_checked"]
_SUMMARY_FIELDS = ["current", "min", "mean", "last_checked"]


class ItemsTab:
    """Reads tracked items and writes their summary statistics back."""

    def __init__(self, gsheet, sheet_name: str, logger: logging.Logger) -> None:
        self.logger = get_child_logger(logger, __class__.__name__)
        self._gsheet = gsheet
        self._sheet_name = sheet_name

    def read(self) -> list[Item]:
        """Return the valid, de-duplicated items listed on the tab."""
        frame = self._gsheet.read(sheet_name=self._sheet_name)
        if frame.empty or "item" not in frame.columns or "website" not in frame.columns:
            return []

        items: list[Item] = []
        seen: set[tuple[str, str]] = set()
        for position, row in enumerate(frame.to_dict("records"), start=2):
            name = str(row.get("item") or "").strip()
            website = str(row.get("website") or "").strip()
            if not name or not website:
                self.logger.warning(f"Row {position}: blank item or website, skipping")
                continue
            key = (name, website)
            if key in seen:
                self.logger.warning(f"Row {position}: duplicate '{name}', skipping")
                continue
            seen.add(key)
            items.append(Item(name=name, website=website))
        return items

    def write_summary(self, items: list[Item], summary: pd.DataFrame) -> None:
        """Rewrite the tab with each item's latest statistics."""
        lookup: dict[tuple[str, str], dict] = {}
        if not summary.empty:
            lookup = {
                (row["item"], row["website"]): row
                for row in summary.to_dict("records")
            }

        rows = []
        for item in items:
            stats = lookup.get((item.name, item.website), {})
            row = {"item": item.name, "website": item.website}
            for field in _SUMMARY_FIELDS:
                row[field] = stats.get(field, "")
            rows.append(row)

        frame = pd.DataFrame(rows, columns=COLUMNS)
        # write(), not update(): this tab shrinks when the user deletes an item,
        # and without the clear a deletion would leave a duplicated last row.
        self._gsheet.write(sheet_name=self._sheet_name, df=frame)
        self.logger.info(f"Wrote summary for {len(rows)} items")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_items_tab.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add src/price_monitor/sheets/items_tab.py tests/unit/test_items_tab.py
git commit -m "feat: add items tab reader and summary writer"
```

---

### Task 10: Controller, CLI, and README

**Files:**
- Create: `src/price_monitor/price_monitor.py`
- Create: `README.md`
- Create: `config/config.example.json`
- Test: `tests/unit/test_price_monitor.py`

**Interfaces:**
- Consumes: everything from Tasks 1-9
- Produces: `PriceMonitor(secrets: Path, logger)` with `update()`, `poll()`, `run()`, `start()`, `stop()`; `args_parser() -> argparse.ArgumentParser`; `main()`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_price_monitor.py`:

```python
import logging
from datetime import datetime

import pandas as pd
import pytest

from price_monitor.models import Item, PriceReading, PriceStatus
from price_monitor.price_monitor import PriceMonitor, args_parser


class FakeSheet:
    def __init__(self, tabs: dict[str, pd.DataFrame]):
        self.tabs = tabs
        self.modified = False

    def read(self, sheet_name: str) -> pd.DataFrame:
        return self.tabs.get(sheet_name, pd.DataFrame()).copy()

    def write(self, sheet_name: str, df: pd.DataFrame) -> None:
        self.tabs[sheet_name] = df.copy()

    def update(self, sheet_name: str, df: pd.DataFrame) -> None:
        self.tabs[sheet_name] = df.copy()

    def is_modified(self) -> bool:
        return self.modified


class FakeSearcher:
    def __init__(self):
        self.priced: list[str] = []

    def price(self, item: Item, last_price, timestamp) -> PriceReading:
        self.priced.append(item.name)
        return PriceReading(
            timestamp=timestamp,
            item=item.name,
            website=item.website,
            price=100.0,
            currency="GBP",
            status=PriceStatus.OK,
            source_url="https://shop.example/w",
        )


@pytest.fixture
def logger():
    return logging.getLogger("test")


def _monitor(sheet, searcher, logger) -> PriceMonitor:
    return PriceMonitor.for_test(gsheet=sheet, searcher=searcher, logger=logger)


def _items(rows):
    return pd.DataFrame(rows, columns=["item", "website"])


def test_update_prices_every_item(logger):
    sheet = FakeSheet({"Items": _items([
        {"item": "Widget", "website": "shop.example"},
        {"item": "Gadget", "website": "shop.example"},
    ])})
    searcher = FakeSearcher()
    _monitor(sheet, searcher, logger).update()
    assert searcher.priced == ["Widget", "Gadget"]
    assert len(sheet.tabs["Prices"]) == 2


def test_update_writes_the_summary_back(logger):
    sheet = FakeSheet({"Items": _items([{"item": "Widget", "website": "shop.example"}])})
    _monitor(sheet, FakeSearcher(), logger).update()
    assert sheet.tabs["Items"].iloc[0]["current"] == 100.0


def test_poll_does_nothing_when_the_sheet_is_unchanged(logger):
    sheet = FakeSheet({"Items": _items([{"item": "Widget", "website": "shop.example"}])})
    searcher = FakeSearcher()
    monitor = _monitor(sheet, searcher, logger)
    sheet.modified = False
    monitor.poll()
    assert searcher.priced == []


def test_poll_prices_only_the_new_item(logger):
    sheet = FakeSheet({
        "Items": _items([
            {"item": "Widget", "website": "shop.example"},
            {"item": "Gadget", "website": "shop.example"},
        ]),
        "Prices": pd.DataFrame(
            [{
                "timestamp": "2026-08-20 06:00:00", "item": "Widget",
                "website": "shop.example", "price": "100.00", "currency": "GBP",
                "status": "ok", "source_url": "", "note": "",
            }],
            columns=["timestamp", "item", "website", "price", "currency",
                     "status", "source_url", "note"],
        ),
    })
    searcher = FakeSearcher()
    monitor = _monitor(sheet, searcher, logger)
    sheet.modified = True
    monitor.poll()
    assert searcher.priced == ["Gadget"]


def test_a_failing_item_does_not_stop_the_others(logger):
    class Exploding(FakeSearcher):
        def price(self, item, last_price, timestamp):
            if item.name == "Widget":
                raise RuntimeError("boom")
            return super().price(item, last_price, timestamp)

    sheet = FakeSheet({"Items": _items([
        {"item": "Widget", "website": "shop.example"},
        {"item": "Gadget", "website": "shop.example"},
    ])})
    searcher = Exploding()
    _monitor(sheet, searcher, logger).update()
    statuses = list(sheet.tabs["Prices"]["status"])
    assert "error" in statuses
    assert "ok" in statuses


def test_last_price_is_passed_to_the_searcher(logger):
    seen = {}

    class Recording(FakeSearcher):
        def price(self, item, last_price, timestamp):
            seen[item.name] = last_price
            return super().price(item, last_price, timestamp)

    sheet = FakeSheet({
        "Items": _items([{"item": "Widget", "website": "shop.example"}]),
        "Prices": pd.DataFrame(
            [{
                "timestamp": "2026-08-20 06:00:00", "item": "Widget",
                "website": "shop.example", "price": "80.00", "currency": "GBP",
                "status": "ok", "source_url": "", "note": "",
            }],
            columns=["timestamp", "item", "website", "price", "currency",
                     "status", "source_url", "note"],
        ),
    })
    _monitor(sheet, Recording(), logger).update()
    assert seen["Widget"] == 80.0


def test_parser_requires_secrets():
    with pytest.raises(SystemExit):
        args_parser().parse_args([])


def test_parser_accepts_once_flag():
    args = args_parser().parse_args(["--secrets", "/tmp/s", "--once"])
    assert args.once is True
    assert str(args.secrets) == "/tmp/s"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_price_monitor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'price_monitor.price_monitor'`

- [ ] **Step 3: Write the implementation**

`src/price_monitor/price_monitor.py`:

```python
"""PriceMonitor controller and CLI entry point.

Owns timing and orchestration: a six-hourly full refresh of every tracked item
and a faster poll that prices only items newly added to the sheet. All state
lives in the spreadsheet, so a restart loses nothing.
"""

import argparse
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from google_drive_api import GoogleSheetInterface
from py_utils.logger import consoleLogger, get_child_logger

from price_monitor.app_config import PriceCtrl, load_price_config
from price_monitor.models import Item, PriceReading, PriceStatus
from price_monitor.search.searcher import PriceSearcher
from price_monitor.sheets.history_tab import HistoryTab
from price_monitor.sheets.items_tab import ItemsTab

# Wake at least this often so stop() is honoured promptly even when the next
# scheduled event is hours away.
_MAX_SLEEP_S = 20.0


class PriceMonitor:
    """Polls a sheet of tracked items and records their prices over time."""

    def __init__(self, secrets: Path, logger: logging.Logger) -> None:
        self.logger = get_child_logger(logger, __class__.__name__)
        # Must precede any decryption: EncStr fields are decrypted with the key
        # this puts into the environment.
        load_dotenv(dotenv_path=(secrets / ".env"))
        config = load_price_config(secrets / "config.json")

        self.ctrl: PriceCtrl = config.price_ctrl
        gsheet = GoogleSheetInterface(config=config.drive_config, logger=logger)
        self._init_parts(
            gsheet=gsheet,
            searcher=PriceSearcher(config.llm_config, config.price_ctrl, logger),
        )

    @classmethod
    def for_test(cls, gsheet, searcher, logger: logging.Logger) -> "PriceMonitor":
        """Build a controller around fakes, skipping config and network setup."""
        monitor = cls.__new__(cls)
        monitor.logger = get_child_logger(logger, cls.__name__)
        monitor.ctrl = PriceCtrl(request_delay_s=0.0)
        monitor._init_parts(gsheet=gsheet, searcher=searcher)
        return monitor

    def _init_parts(self, gsheet, searcher) -> None:
        self._searcher = searcher
        self.items_tab = ItemsTab(gsheet, self.ctrl.items_sheet, self.logger)
        self.history = HistoryTab(gsheet, self.ctrl.history_sheet, self.logger)
        self._gsheet = gsheet
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="PriceMonitor")

    def update(self) -> None:
        """Price every tracked item and rewrite the summary."""
        items = self.items_tab.read()
        if not items:
            self.logger.info("No items to price")
            return
        self.logger.info(f"Pricing {len(items)} items")
        self._price_and_record(items)

    def poll(self) -> None:
        """Price only items added to the sheet since the last check."""
        if not self._gsheet.is_modified():
            return
        items = self.items_tab.read()
        known = self.history.known_items()
        new_items = [i for i in items if (i.name, i.website) not in known]
        if not new_items:
            return
        self.logger.info(f"Found {len(new_items)} new items")
        self._price_and_record(new_items)

    def _price_and_record(self, items: list[Item]) -> None:
        last_prices = self.history.last_prices()
        readings: list[PriceReading] = []

        for index, item in enumerate(items):
            if index and self.ctrl.request_delay_s:
                self.stop_event.wait(self.ctrl.request_delay_s)
            readings.append(
                self._price_one(item, last_prices.get((item.name, item.website)))
            )

        self.history.append(readings)
        self.items_tab.write_summary(self.items_tab.read(), self.history.summarise())

    def _price_one(self, item: Item, last_price: float | None) -> PriceReading:
        timestamp = datetime.now().replace(microsecond=0)
        try:
            return self._searcher.price(item, last_price, timestamp)
        except Exception as exc:
            # One unreachable site must never abort the rest of the run.
            self.logger.warning(f"Pricing '{item.name}' failed: {exc}")
            return PriceReading(
                timestamp=timestamp,
                item=item.name,
                website=item.website,
                price=None,
                currency=self.ctrl.currency,
                status=PriceStatus.ERROR,
                note=str(exc),
            )

    def run(self) -> None:
        """Timer loop: a full refresh on a slow clock, new-item polling on a fast one."""
        refresh_interval_s = self.ctrl.refresh_rate_h * 3600
        poll_interval_s = self.ctrl.poll_rate_m * 60

        last_refresh = 0.0  # zero so a full refresh fires immediately on start
        last_poll = time.monotonic()

        while not self.stop_event.is_set():
            now = time.monotonic()
            if last_refresh == 0.0 or now - last_refresh >= refresh_interval_s:
                try:
                    self.update()
                except Exception as exc:
                    self.logger.warning(f"Update failed (will retry): {exc}")
                last_refresh = time.monotonic()

            if time.monotonic() - last_poll >= poll_interval_s:
                try:
                    self.poll()
                except Exception as exc:
                    self.logger.warning(f"Poll failed (will retry): {exc}")
                last_poll = time.monotonic()

            next_refresh_in = refresh_interval_s - (time.monotonic() - last_refresh)
            next_poll_in = poll_interval_s - (time.monotonic() - last_poll)
            self.stop_event.wait(
                max(0.0, min(next_refresh_in, next_poll_in, _MAX_SLEEP_S))
            )

    def start(self) -> None:
        self.thread.start()
        self.logger.info("Start thread")

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join()
        self.logger.info("Stop thread")


def args_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Track item prices from a Google Sheet using a search-grounded LLM"
    )
    parser.add_argument(
        "--secrets",
        type=Path,
        required=True,
        help="Directory holding config.json and .env",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single update and exit instead of starting the poll loop",
    )
    return parser


def main() -> None:
    logger = consoleLogger("main")
    args = args_parser().parse_args()
    monitor = None
    try:
        monitor = PriceMonitor(secrets=args.secrets.expanduser(), logger=logger)
        if args.once:
            monitor.update()
            return
        monitor.start()
        monitor.thread.join()
    except KeyboardInterrupt:
        print("Caught Ctrl+C! Exiting gracefully.")
    finally:
        if monitor is not None and not args.once:
            monitor.stop()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_price_monitor.py -v`
Expected: 8 passed

- [ ] **Step 5: Add the example config and README**

`config/config.example.json`:

```json
{
  "config": {
    "drive_config": {
      "service_file": "service_account.json",
      "remote_file": "PriceMonitor"
    },
    "llm_config": {
      "provider": "gemini_search",
      "model": "gemini-3.7-flash",
      "api_key": "your-gemini-api-key",
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
  }
}
```

`README.md` must cover: what the tool does; the two-tab sheet contract with the `website` column accepting either a domain or a full product URL; the install order (`pip install -e ../AIInterface` first, because `llmbridge` has no remote yet, then `pip install -e ".[dev]"`); creating the secrets directory with `config.json` and `.env` holding `ENCRYPTION_KEY`; sharing the spreadsheet with the service account email; running `price-monitor --secrets <dir>` and `--once`; the status values and what each means; and a note that call two is deliberately un-grounded because Gemini's grounding and structured output are unreliable together.

- [ ] **Step 6: Run the whole suite and the linters**

```bash
pytest
black --check src tests
isort --check-only src tests
flake8 src tests
```
Expected: all tests pass; all three linters clean. Fix any findings before committing.

- [ ] **Step 7: Commit**

```bash
git add src/price_monitor/price_monitor.py tests/unit/test_price_monitor.py README.md config/config.example.json
git commit -m "feat: add controller, CLI, and documentation"
```

---

### Task 11: Integration tests

**Files:**
- Create: `tests/integration/test_live_search.py`
- Create: `tests/integration/conftest.py`
- Modify: `.gitignore` (confirm `config/` is ignored but `config/config.example.json` is committed)

**Interfaces:**
- Consumes: `PriceSearcher` (T7), `load_price_config` (T2), `Item` (T1)
- Produces: nothing consumed by later tasks

**Note:** `.gitignore` currently ignores `config/`, which would exclude the example committed in Task 10. Change that line to `config/*` plus `!config/config.example.json`, and verify with `git check-ignore -v config/config.example.json` returning nothing.

- [ ] **Step 1: Write the integration tests**

`tests/integration/conftest.py`:

```python
"""Fixtures for tests that call real external services."""

import logging
import os
from pathlib import Path

import pytest

from price_monitor.app_config import load_price_config


@pytest.fixture(scope="session")
def secrets_dir() -> Path:
    """Secrets directory from PRICE_MONITOR_SECRETS, or skip the test."""
    raw = os.environ.get("PRICE_MONITOR_SECRETS")
    if not raw:
        pytest.skip("PRICE_MONITOR_SECRETS is not set")
    path = Path(raw).expanduser()
    if not (path / "config.json").exists():
        pytest.skip(f"No config.json in {path}")
    return path


@pytest.fixture(scope="session")
def config(secrets_dir: Path):
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=secrets_dir / ".env")
    return load_price_config(secrets_dir / "config.json")


@pytest.fixture(scope="session")
def logger() -> logging.Logger:
    return logging.getLogger("integration")
```

`tests/integration/test_live_search.py`:

```python
"""Live checks against the real Gemini API and Google Sheets.

Run with: pytest -m integration
Requires PRICE_MONITOR_SECRETS to point at a configured secrets directory.
"""

from datetime import datetime

import pytest

from price_monitor.models import Item, PriceStatus
from price_monitor.search.searcher import PriceSearcher

pytestmark = pytest.mark.integration


def test_grounded_lookup_returns_a_usable_reading(config, logger):
    searcher = PriceSearcher(config.llm_config, config.price_ctrl, logger)
    item = Item(name="Sony WH-1000XM5 headphones", website="amazon.co.uk")
    reading = searcher.price(item, None, datetime.now().replace(microsecond=0))

    assert reading.status in {
        PriceStatus.OK,
        PriceStatus.SUSPECT,
        PriceStatus.NOT_FOUND,
        PriceStatus.WRONG_CURRENCY,
    }, f"unexpected status {reading.status}: {reading.note}"

    if reading.status in {PriceStatus.OK, PriceStatus.SUSPECT}:
        assert reading.price is not None and reading.price > 0
        assert reading.source_url.startswith("http")


def test_sheet_round_trip(config, logger):
    from google_drive_api import GoogleSheetInterface

    from price_monitor.sheets.items_tab import ItemsTab

    gsheet = GoogleSheetInterface(config=config.drive_config, logger=logger)
    items = ItemsTab(gsheet, config.price_ctrl.items_sheet, logger).read()
    assert isinstance(items, list)
```

- [ ] **Step 2: Verify integration tests are excluded by default**

Run: `pytest`
Expected: unit tests run; integration tests are deselected by the `-m 'not integration'` default.

- [ ] **Step 3: Run the integration suite against real services**

Run: `PRICE_MONITOR_SECRETS=~/secrets pytest -m integration -v`
Expected: both tests pass, or skip cleanly when the variable is unset. A `PARSE_ERROR` here means the grounding/JSON split needs revisiting — report it rather than loosening the assertion.

- [ ] **Step 4: Commit**

```bash
git add tests/integration .gitignore
git commit -m "test: add opt-in integration tests"
```

---

## Verification

After Task 11, confirm the whole thing holds together:

```bash
pytest                      # full unit suite green
black --check src tests
isort --check-only src tests
flake8 src tests
pip install -e ".[dev]" && price-monitor --help
```

Then a real single run against a live sheet:

```bash
price-monitor --secrets ~/secrets --once
```

Expected: the Prices tab gains one row per item, and the Items tab gains current/min/mean values. Check that `source_url` on each row points at a real product page — that is the audit trail the whole design rests on.
