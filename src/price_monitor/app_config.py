"""Configuration schema for PriceMonitor.

Defines the dataclasses passed as ``schema`` to ``py_utils.config.load_config``
so that ``config.json`` gets proper types and automatic decryption of the API
key. Every field outside ``drive_config`` carries a default, so a config file
missing a section still loads.
"""

from dataclasses import dataclass, field
from pathlib import Path

from google_drive_api import DriveConfig
from py_utils.config import EncStr, load_config


@dataclass
class LLMConfig:
    """Credentials and model settings shared by both calls of a lookup.

    ``provider`` names the grounded search provider used for call 1. Call 2
    always uses the stock ``gemini`` provider with the same model and key.

    ``grounded`` switches call 1 between ``provider`` and the un-grounded
    stock ``gemini`` provider. Turning it off makes the lookup work on a key
    whose project has no Google Search grounding quota, at the cost of the
    audit trail: an un-grounded model has no cited sources, so ``source_url``
    is whatever it claims rather than a page it actually read.
    """

    api_key: EncStr
    grounded: bool = True
    provider: str = "gemini_search"
    model: str = "gemini-3.7-flash"
    max_tokens: int = 1024
    temperature: float = 0.0
    timeout: float = 60.0


@dataclass
class PriceCtrl:
    """Timing, sheet names, and validation thresholds."""

    refresh_rate_h: float = 6.0
    poll_rate_m: float = 5.0
    items_sheet: str = "Items"
    history_sheet: str = "Prices"
    currency: str = "GBP"
    # Applied only when the reported price excludes VAT, so recorded history is
    # always VAT-inclusive and comparable run to run. 0.2 = UK standard rate.
    vat_rate: float = 0.2
    suspect_threshold: float = 0.5
    # Width of each price-drop alert band below the mean. A falling price is
    # announced once per band it gives up, so a slow slide keeps reporting
    # without a message on every refresh. 0.05 = one message per 5% lost.
    drop_step: float = 0.05
    max_plausible_price: float = 100000.0
    request_delay_s: float = 2.0


@dataclass
class MsgConfig:
    """Connection settings for the MsgServer log forwarder.

    All fields have defaults so a ``config.json`` without a ``msg_config``
    section loads unchanged; the defaults assume the server runs on the same
    host as PriceMonitor.
    """

    handle: str = "pm"
    router_endpoint: str = "tcp://127.0.0.1:5555"
    sub_endpoint: str = "tcp://127.0.0.1:5556"
    timeout_ms: int = 2000


@dataclass
class Config:
    """Top-level ``config`` section of config.json."""

    drive_config: DriveConfig
    llm_config: LLMConfig
    price_ctrl: PriceCtrl = field(default_factory=PriceCtrl)
    msg_config: MsgConfig = field(default_factory=MsgConfig)


def load_price_config(path: Path) -> Config:
    """Load and validate config.json into a :class:`Config`."""
    return load_config(path, key="config", schema=Config)
