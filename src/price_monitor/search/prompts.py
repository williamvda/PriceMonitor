"""Prompt templates for single-supplier and market-wide price lookups.

Single-supplier: call one asks a grounded model for a prose report; call two
converts that report into JSON with no grounding in play.

Market-wide: call one asks a grounded model to sweep UK retailers for the best
price; call two converts the sweep report into JSON with a list of candidates.

Keeping the templates here makes them reviewable without reading the search
plumbing.
"""

from price_monitor.models import Item

_SITE_TEMPLATE = """Find the current price of this product.

Product: {item}
Site to search: {website}

Search that site and report:
- the price shown right now, with its currency
- whether that price includes VAT (sales tax)
- the exact URL of the page you read the price from
- whether it is in stock

If the page shows both a VAT-inclusive and a VAT-exclusive price, report the
VAT-inclusive one and say so. If it shows only one, report it and say which
kind it is. Do not calculate VAT yourself.

If you cannot find the product on that site, say so plainly.
Do not guess a price. Report only a price you actually saw."""

_PAGE_TEMPLATE = """Find the current price of the product on this page.

Product: {item}
Page: {website}

Read that page and report:
- the price shown right now, with its currency
- whether that price includes VAT (sales tax)
- the exact URL you read the price from
- whether it is in stock

If the page shows both a VAT-inclusive and a VAT-exclusive price, report the
VAT-inclusive one and say so. If it shows only one, report it and say which
kind it is. Do not calculate VAT yourself.

If you cannot read that page — it is blocked, requires a check you cannot
pass, or shows no price — then search for this product's current price at the
same retailer instead. Report the price the search results show, name the URL
it came from, and say that you fell back to search.

If neither the page nor a search shows a price, say so plainly.
Do not guess a price. Report only a price you actually saw on the page or in
search results."""

_FORMAT_TEMPLATE = """Convert the report below into one JSON object and nothing else.

Schema:
{{
  "price": <number or null>,
  "currency": "<ISO 4217 code>",
  "url": "<string>",
  "in_stock": <true or false>,
  "vat_included": <true or false>,
  "found": <true or false>,
  "note": "<string>"
}}

Rules:
- Set found to false when the report gives no price; price is then null.
- vat_included records whether the reported price already includes VAT. Set it
  from what the report says; when the report does not say, set it to true so no
  tax is added to a price that may already include it. Never adjust price
  yourself — report the figure as given and let vat_included describe it.
- price is a plain number: no currency symbol, no thousands separators.
- url is the page the price was read from. Use one of the known source URLs
  below if the report does not name a page.
- note explains why found is false; otherwise use an empty string.
- Output the JSON object only. No markdown fences, no commentary.

Known source URLs: {urls}

Report:
{report}"""


def search_prompt(item: Item) -> str:
    """Build the call-one prompt, choosing site-search or single-page wording."""
    template = _PAGE_TEMPLATE if item.is_direct_url else _SITE_TEMPLATE
    return template.format(item=item.name, website=item.website)


def format_prompt(report: str, urls: list[str]) -> str:
    """Build the call-two prompt that turns a prose report into JSON."""
    return _FORMAT_TEMPLATE.format(
        urls=", ".join(urls) if urls else "none", report=report
    )


_MARKET_TEMPLATE = """Find the current UK price of this product across retailers.

Product: {item}

Find current UK prices for this product from as many different UK retailers as
you can — aim for three to six. For each retailer report:
- the retailer's name
- the price shown right now, with its currency
- whether that price includes VAT (sales tax)
- whether it is in stock
- the exact URL of the page you read the price from

For each retailer also report its Trustpilot rating out of 5 and how many
reviews that rating is based on. If the retailer has no Trustpilot page, use its
Google review rating instead and say which of the two you used. If you cannot
find a rating, say unknown — do not estimate one.

Only list the same product — the same model and variant. Do not list
accessories, bundles, multipacks, refurbished or used items unless the product
name asks for one. Do not guess a price; list only prices you actually saw. If
you can find it at only one retailer, list only that one."""

_MARKET_FORMAT_TEMPLATE = (
    """Convert the report below into one JSON object and nothing else.

Schema:
{{
  "candidates": [
    {{
      "supplier": "<retailer name>",
      "price": <number>,
      "currency": "<ISO 4217 code>",
      "url": "<string>",
      "in_stock": <true or false>,
      "vat_included": <true or false>,
      "rating": <number or null>,
      "rating_count": <integer or null>,
      "rating_source": "trustpilot" | "google" | ""
    }}
  ],
  "note": "<string>"
}}

Rules:
- One entry per retailer the report names with a price. If the report gives no
  price at any retailer, return an empty candidates list and say why in note.
- vat_included records whether that retailer's price already includes VAT. When
  the report does not say, set it to true so no tax is added to a price that may
  already include it. Never adjust a price yourself.
- price is a plain number: no currency symbol, no thousands separators.
- rating and rating_count are null when the report gives no rating for that
  retailer. Never invent a rating.
- rating_source is "trustpilot" or "google" — whichever the report used — and ""
  when there is no rating.
- Output the JSON object only. No markdown fences, no commentary.

Known source URLs: {urls}

Report:
{report}"""
)


def market_prompt(item: Item) -> str:
    """Build the call-one prompt for a UK-wide sweep across retailers."""
    return _MARKET_TEMPLATE.format(item=item.name)


def market_format_prompt(report: str, urls: list[str]) -> str:
    """Build the call-two prompt that turns a sweep report into JSON."""
    return _MARKET_FORMAT_TEMPLATE.format(
        urls=", ".join(urls) if urls else "none", report=report
    )
