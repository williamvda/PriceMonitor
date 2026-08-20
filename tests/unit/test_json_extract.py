"""Tests for JSON extraction from LLM responses."""

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
