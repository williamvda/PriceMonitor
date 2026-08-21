"""Prompt templates for the two-call price lookup.

Call one asks a grounded model for a prose report; call two converts that
report into JSON with no grounding in play. Keeping the templates here makes
them reviewable without reading the search plumbing.
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

If the page does not show a price, say so plainly.
Do not guess a price. Report only a price you actually saw."""

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
