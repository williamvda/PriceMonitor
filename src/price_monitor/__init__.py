"""PriceMonitor: track item prices from a Google Sheet using a search-grounded LLM."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("price-monitor")
except PackageNotFoundError:  # a source tree that was never pip-installed
    __version__ = "unknown"
