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


def _web_chunks(raw: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = raw.get("candidates") or []
    if not candidates:
        return []
    chunks = candidates[0].get("groundingMetadata", {}).get("groundingChunks", [])
    return [chunk.get("web", {}) for chunk in chunks if isinstance(chunk, dict)]


def grounding_urls(raw: dict[str, Any]) -> list[str]:
    """Return the source URIs Gemini cited for a grounded response, in order."""
    return [uri for web in _web_chunks(raw) if (uri := web.get("uri", ""))]


def grounding_sites(raw: dict[str, Any]) -> list[str]:
    """Return the retailers Gemini cited, as site names.

    Prefers each chunk's ``title``, which Gemini fills with the site's name,
    over its ``uri`` — the URIs are Vertex redirect links that never resemble
    the retailer's own address, so matching a product URL against them can
    never succeed. Live sweeps scored every winner "uncited url" until this
    was understood.
    """
    sites: list[str] = []
    for web in _web_chunks(raw):
        title = str(web.get("title") or "").strip().lower()
        if not title:
            continue
        site = title[4:] if title.startswith("www.") else title
        # De-duplicated: Gemini cites the same site once per passage it drew
        # on, so the raw chunk count says how many passages it read, not how
        # many places it looked. Nothing downstream wants the repeats, and
        # counting them badly overstates the breadth of a sweep.
        if site not in sites:
            sites.append(site)
    return sites


def was_truncated(raw: dict[str, Any]) -> bool:
    """True when Gemini stopped at ``max_tokens`` rather than finishing.

    A truncated reply still carries text, so it is indistinguishable from a
    complete one unless the finish reason is read. For a sweep that means the
    retailers the model had not yet named are simply absent, with every
    downstream number looking healthy.
    """
    candidates = raw.get("candidates") or []
    return bool(candidates) and candidates[0].get("finishReason") == "MAX_TOKENS"


ProviderRegistry.register("gemini_search", GeminiSearchProvider)
