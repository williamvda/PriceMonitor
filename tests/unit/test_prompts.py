"""Prompt template tests."""

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
