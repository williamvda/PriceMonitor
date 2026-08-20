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
