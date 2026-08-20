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
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"malformed JSON in reply: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("reply JSON was not an object")
    return parsed


def _strip_fences(text: str) -> str:
    """Remove a surrounding markdown code fence, if present."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    # Single-line fence: strip opening and closing backticks
    if stripped.count("```") == 2 and "\n" not in stripped:
        # Format: ```[label] content```
        # Find the position of the first closing ```
        first_fence_end = stripped.find("```", 3)
        if first_fence_end != -1:
            # Extract content between the fences
            return stripped[3:first_fence_end].strip()
    # Multi-line fence: strip the opening and closing lines
    lines = stripped.splitlines()[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _first_balanced_object(text: str) -> str | None:
    """Return the first brace-balanced ``{...}`` span, ignoring braces in strings.

    Single-pass algorithm using a stack to track opening braces. Returns the
    complete span with the smallest starting index (handles stray braces and
    nested objects). Correctly ignores braces inside string literals.
    """
    stack: list[int] = []  # Stack of opening brace positions
    first_complete_span: tuple[int, int] | None = (
        None  # (start, end) of complete object with smallest start
    )

    in_string = False
    escaped = False

    for index, char in enumerate(text):
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
            stack.append(index)
        elif char == "}":
            if stack:
                start = stack.pop()
                # We found a complete span; prefer the one with the smallest start index
                if first_complete_span is None or start < first_complete_span[0]:
                    first_complete_span = (start, index)

    if first_complete_span is None:
        return None

    start, end = first_complete_span
    return text[start : end + 1]
