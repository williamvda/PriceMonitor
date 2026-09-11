"""Prompt template tests."""

from price_monitor.models import Item
from price_monitor.search.prompts import (
    format_prompt,
    market_format_prompt,
    market_prompt,
    search_prompt,
)


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


def test_page_prompt_degrades_to_search_when_the_page_is_blocked():
    """Bot-gated retailers (Cloudflare and friends) serve a challenge page to
    any automated fetch, so a page-only instruction fails outright. The
    fallback keeps those items working via the search index."""
    prompt = search_prompt(Item(name="X", website="https://a.com/p"))
    assert "blocked" in prompt
    assert "fell back to search" in prompt


def test_page_prompt_fallback_still_forbids_guessing():
    """The degraded path must reach search results, never model recall — a
    remembered price would look like real data in the history tab."""
    prompt = search_prompt(Item(name="X", website="https://a.com/p"))
    assert "Do not guess a price" in prompt
    assert "actually saw on the page or in\nsearch results" in prompt


def test_site_prompt_is_unchanged_by_the_page_fallback():
    prompt = search_prompt(Item(name="X", website="a.com"))
    assert "Search that site" in prompt
    assert "fell back to search" not in prompt


def test_market_prompt_names_the_product_and_asks_for_a_set():
    prompt = market_prompt(Item(name="Widget 3000", website="shop.example"))
    assert "Widget 3000" in prompt
    assert "three to six" in prompt


def test_market_prompt_permits_a_single_retailer():
    """Without this the model pads the list, which corrupts the agreement score."""
    prompt = market_prompt(Item(name="Widget", website="shop.example"))
    assert "only one retailer" in prompt


def test_market_prompt_forbids_estimating_a_rating():
    prompt = market_prompt(Item(name="Widget", website="shop.example"))
    assert "Trustpilot" in prompt
    assert "do not estimate one" in prompt


def test_market_prompt_excludes_the_tracked_site_from_its_wording():
    """The sweep is market-wide; the item's own website is not part of it."""
    assert "shop.example" not in market_prompt(
        Item(name="Widget", website="shop.example")
    )


def test_market_format_prompt_carries_the_report_and_urls():
    prompt = market_format_prompt("Argos has it at 99.99", ["https://argos.co.uk/w"])
    assert "Argos has it at 99.99" in prompt
    assert "https://argos.co.uk/w" in prompt
    assert "candidates" in prompt


def test_market_format_prompt_renders_no_urls_as_none():
    assert "none" in market_format_prompt("report", [])
