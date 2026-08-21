"""Test the Gemini search-grounded provider."""

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
    assert body["contents"] == [{"role": "user", "parts": [{"text": "find a price"}]}]
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
