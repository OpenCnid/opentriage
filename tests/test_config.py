"""Tests for config system."""

from pathlib import Path

from opentriage.config import Config, DEFAULT_CONFIG


def test_config_defaults():
    cfg = Config()
    assert cfg.provider["backend"] == "anthropic"
    assert cfg.budget["max_retries_per_event"] == 2
    assert cfg.triage["scan_window_hours"] == 2


def test_config_save_load(tmp_path: Path):
    cfg = Config()
    cfg.provider["backend"] = "openai"
    path = tmp_path / "config.toml"
    cfg.save(path)

    loaded = Config.load(path)
    assert loaded.provider["backend"] == "openai"
    # Defaults preserved for unset keys
    assert loaded.budget["max_daily_cost_usd"] == 20.0


def test_config_get_set():
    cfg = Config()
    assert cfg.get("provider.backend") == "anthropic"
    cfg.set("provider.backend", "openai")
    assert cfg.get("provider.backend") == "openai"

    cfg.set("budget.max_retries_per_event", "5")
    assert cfg.get("budget.max_retries_per_event") == 5

    cfg.set("budget.max_daily_cost_usd", "30.0")
    assert cfg.get("budget.max_daily_cost_usd") == 30.0


def test_config_load_missing(tmp_path: Path):
    cfg = Config.load(tmp_path / "nonexistent.toml")
    assert cfg.provider["backend"] == "anthropic"


def test_config_get_section():
    cfg = Config()
    section = cfg.get("provider")
    assert isinstance(section, dict)
    assert "backend" in section
