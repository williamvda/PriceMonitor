"""Prompt templates for the market-wide price sweep.

Call one asks a grounded model to sweep UK retailers for a product's price;
call two converts the sweep report into JSON with a list of candidates.

Keeping the templates here makes them reviewable without reading the search
plumbing.
"""

from price_monitor.models import Item

_MARKET_TEMPLATE = """Find the current UK price of this product across retailers.

Product: {item}

Search thoroughly and list every UK retailer you can find selling it. The
point of the search is to find the cheapest, so do not stop at the first few
results — a retailer further down may be the cheap one. Look beyond the
obvious: large marketplaces, specialist retailers and the manufacturer's own
shop all count. For each retailer report:
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
name asks for one. Do not guess a price; list only prices you actually saw.

If, after searching properly, the product really is sold by only one retailer,
list only that one — but do not pad the list with retailers you did not check,
and do not stop searching early just because you already have a few."""

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
