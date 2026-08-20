"""Tests for JSON extraction from LLM responses."""

import time

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


def test_non_dict_json_raises():
    """JSON that is not an object (bare array) must raise ValueError."""
    with pytest.raises(ValueError):
        extract_json('[1, 2, 3]')


def test_deeply_nested_json_does_not_escape():
    """Deeply nested JSON that causes RecursionError must raise ValueError, not escape."""
    # Build a deeply nested object (~1200 levels deep, beyond Python's default recursion limit)
    text = '{"a":' * 1200 + '1' + '}' * 1200
    with pytest.raises(ValueError):
        extract_json(text)


def test_stray_brace_before_real_object():
    """Stray unmatched brace before the real object should not prevent extraction."""
    text = 'note: { and the data is {"price": 5.0}'
    assert extract_json(text) == {"price": 5.0}


def test_stray_brace_before_fenced_object():
    """Stray unmatched brace before a fenced object should not prevent extraction."""
    text = 'note: {\n```json\n{"price": 5.0}\n```'
    assert extract_json(text) == {"price": 5.0}


def test_single_line_fence():
    """Single-line fence with no internal newlines must not be destroyed."""
    text = '```json {"price": 1.0}```'
    assert extract_json(text) == {"price": 1.0}


def test_brace_inside_string_literal_not_corrupted():
    """A brace inside a string literal must not be treated as a span boundary.

    This guards against the corruption case where a retry-based scanner could
    select a brace inside a string, returning corrupted data from inside a
    string literal rather than the real JSON object.
    """
    # The opening brace inside the "note" string value never closes; the whole
    # expression is unterminated and should raise.
    text = '{"note": "z{ "ok": 5} trailing junk'
    with pytest.raises(ValueError):
        extract_json(text)


def test_performance_on_many_stray_braces():
    """Large inputs with many stray braces must not cause O(n²) behavior.

    A garbled reply with many unmatched braces (plausible from truncated or
    repeated model output) must complete within a generous wall-clock bound.
    The stack-based scanner is O(n) and should handle this easily.
    """
    # Create a reply with 2000 stray braces followed by a real object.
    # With O(n²) retry behavior, this would take several seconds; with O(n),
    # it should be nearly instant.
    text = "{" * 2000 + '{"price": 5.0}'
    start_time = time.time()
    result = extract_json(text)
    elapsed = time.time() - start_time

    assert result == {"price": 5.0}
    # Generous bound: should complete in well under 1 second on any reasonable machine
    assert elapsed < 1.0, f"Extraction took {elapsed:.2f}s, should be instant"
