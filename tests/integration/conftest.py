"""Fixtures for tests that call real external services."""

import logging
import os
from pathlib import Path

import pytest

from price_monitor.app_config import load_price_config


@pytest.fixture(scope="session")
def secrets_dir() -> Path:
    """Secrets directory from PRICE_MONITOR_SECRETS, or skip the test."""
    raw = os.environ.get("PRICE_MONITOR_SECRETS")
    if not raw:
        pytest.skip("PRICE_MONITOR_SECRETS is not set")
    path = Path(raw).expanduser()
    if not (path / "config.json").exists():
        pytest.skip(f"No config.json in {path}")
    return path


@pytest.fixture(scope="session")
def config(secrets_dir: Path):
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=secrets_dir / ".env")
    return load_price_config(secrets_dir / "config.json")


@pytest.fixture(scope="session")
def logger() -> logging.Logger:
    return logging.getLogger("integration")


@pytest.fixture(scope="session")
def gsheet(config, logger):
    """Shared live Sheets client, so multiple tests reuse one credential exchange."""
    from google_drive_api import GoogleSheetInterface

    return GoogleSheetInterface(config=config.drive_config, logger=logger)
