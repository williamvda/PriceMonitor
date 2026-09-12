"""Prompt template tests."""

from price_monitor.models import Item
from price_monitor.search.prompts import market_format_prompt, market_prompt


def test_market_prompt_names_the_product_and_asks_for_a_set():
    prompt = market_prompt(Item(name="Widget 3000"))
    assert "Widget 3000" in prompt
    assert "three to six" in prompt


def test_market_prompt_permits_a_single_retailer():
    """Without this the model pads the list, which corrupts the agreement score."""
    assert "only one retailer" in market_prompt(Item(name="Widget"))


def test_market_prompt_forbids_estimating_a_rating():
    prompt = market_prompt(Item(name="Widget"))
    assert "Trustpilot" in prompt
    assert "do not estimate one" in prompt


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
        "rating",
        "rating_count",
        "rating_source",
    ):
        assert f'"{field}"' in prompt
