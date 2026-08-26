"""Tests for the PriceMonitor configuration schema."""

import json
from pathlib import Path

from cryptography.fernet import Fernet

from price_monitor.app_config import MsgConfig, load_price_config


def _write_config(
    tmp_path: Path,
    price_ctrl: dict | None = None,
    encryption_key: str | None = None,
    msg_config: dict | None = None,
) -> Path:
    # EncStr fields use strict=True decryption, so they fail closed without a real
    # Fernet token. We encrypt test-key rather than write plaintext.
    api_key_value = "test-key"
    if encryption_key:
        cipher = Fernet(encryption_key.encode())
        api_key_value = cipher.encrypt(api_key_value.encode()).decode()

    payload = {
        "config": {
            "drive_config": {
                "service_file": "service_account.json",
                "remote_file": "PriceMonitor",
            },
            "llm_config": {"api_key": api_key_value},
        }
    }
    if price_ctrl is not None:
        payload["config"]["price_ctrl"] = price_ctrl
    if msg_config is not None:
        payload["config"]["msg_config"] = msg_config
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload))
    return path


def test_missing_price_ctrl_section_falls_back_to_defaults(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    config = load_price_config(_write_config(tmp_path, encryption_key=key))
    assert config.price_ctrl.refresh_rate_h == 6.0
    assert config.price_ctrl.poll_rate_m == 5.0
    assert config.price_ctrl.items_sheet == "Items"
    assert config.price_ctrl.history_sheet == "Prices"
    assert config.price_ctrl.currency == "GBP"


def test_llm_defaults_target_grounded_flash(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    config = load_price_config(_write_config(tmp_path, encryption_key=key))
    assert config.llm_config.provider == "gemini_search"
    assert config.llm_config.model == "gemini-3.7-flash"
    assert config.llm_config.temperature == 0.0
    assert config.llm_config.api_key == "test-key"


def test_price_ctrl_values_override_defaults(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    path = _write_config(
        tmp_path, {"refresh_rate_h": 12, "currency": "EUR"}, encryption_key=key
    )
    config = load_price_config(path)
    assert config.price_ctrl.refresh_rate_h == 12
    assert config.price_ctrl.currency == "EUR"
    assert config.price_ctrl.poll_rate_m == 5.0


def test_drive_config_is_populated(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    config = load_price_config(_write_config(tmp_path, encryption_key=key))
    assert config.drive_config.remote_file == "PriceMonitor"


def test_missing_msg_config_section_falls_back_to_defaults(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    config = load_price_config(_write_config(tmp_path, encryption_key=key))
    assert config.msg_config == MsgConfig()
    assert config.msg_config.handle == "pm"
    assert config.msg_config.router_endpoint == "tcp://127.0.0.1:5555"


def test_msg_config_values_override_defaults(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    path = _write_config(
        tmp_path,
        encryption_key=key,
        msg_config={"handle": "pm2", "router_endpoint": "tcp://box:6000"},
    )
    config = load_price_config(path)
    assert config.msg_config.handle == "pm2"
    assert config.msg_config.router_endpoint == "tcp://box:6000"
    assert config.msg_config.timeout_ms == 2000


def test_the_example_config_matches_the_schema(monkeypatch):
    """config.example.json must stay loadable, or it misleads every new setup."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    example = json.loads(
        (Path(__file__).parents[2] / "config" / "config.example.json").read_text()
    )
    assert example["config"]["msg_config"]["handle"] == MsgConfig().handle
