"""Prompt template tests."""

from price_monitor.models import Item
from price_monitor.search.prompts import market_format_prompt, market_prompt


def test_market_prompt_names_the_product_and_asks_for_breadth():
    """The old wording capped ambition at "three to six" and live sweeps
    returned two or three. It now asks for every retailer it can find and
    says why — the cheapest may be further down the results."""
    prompt = market_prompt(Item(name="Widget 3000"))
    assert "Widget 3000" in prompt
    assert "every UK retailer you can find" in prompt
    assert "do not stop at the first few" in prompt
    assert "three to six" not in prompt


def test_market_prompt_permits_a_single_retailer_without_excusing_a_short_search():
    """Honesty about a genuinely single-source product must not double as
    permission to stop early — the padding guard and the breadth instruction
    have to coexist."""
    prompt = market_prompt(Item(name="Widget"))
    assert "only one retailer" in prompt
    assert "do not pad the list" in prompt
    assert "do not stop searching early" in prompt


def test_market_prompt_forbids_guessing_a_price():
    assert "Do not guess a price" in market_prompt(Item(name="Widget"))


def test_market_format_prompt_carries_the_report_and_urls():
    prompt = market_format_prompt("Argos has it at 99.99", ["https://argos.co.uk/w"])
    assert "Argos has it at 99.99" in prompt
    assert "https://argos.co.uk/w" in prompt
    assert "candidates" in prompt


def test_market_format_prompt_renders_no_urls_as_none():
    assert "none" in market_format_prompt("report", [])


def test_market_format_prompt_schema_braces_survive_formatting():
    # The schema block must reach the model as literal JSON, not be consumed
    # by str.format placeholders.
    prompt = market_format_prompt("report", [])
    assert '"price"' in prompt
    assert '"in_stock"' in prompt
    assert prompt.count("{") == prompt.count("}")


def test_market_format_prompt_names_every_schema_field():
    prompt = market_format_prompt("report", [])
    for field in (
        "supplier",
        "price",
        "currency",
        "url",
        "in_stock",
        "vat_included",
    ):
        assert f'"{field}"' in prompt


def test_market_prompt_asks_for_a_terminating_count():
    """The sign-off is the only evidence of how many retailers the prose
    named before the second call converted it — and its absence doubles as a
    truncation check, since a cut-off report cannot carry its own last line."""
    prompt = market_prompt(Item(name="Widget"))
    assert "END: <n> retailers" in prompt
    assert "how many retailers you listed" in prompt


def test_the_sweep_no_longer_asks_for_ratings():
    """Gemini spent grounding passages reading Trustpilot because we asked it
    to. Those passages are wanted for finding shops."""
    prompt = market_prompt(Item(name="Widget"))
    assert "Trustpilot" not in prompt
    assert "rating" not in prompt.lower()


def test_the_format_schema_no_longer_carries_rating_fields():
    schema = market_format_prompt("report", [])
    assert "rating" not in schema
    assert "rating_count" not in schema
    assert "rating_source" not in schema
