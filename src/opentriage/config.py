"""Configuration loading and defaults for OpenTriage."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import tomli_w


DEFAULT_CONFIG: dict[str, Any] = {
    "provider": {
        "backend": "anthropic",
        "cheap_model": "claude-haiku-4-5-20251001",
        "standard_model": "claude-sonnet-4-6",
        "expensive_model": "claude-opus-4-6",
        "api_key_env": "ANTHROPIC_API_KEY",
        "base_url": "",
        "timeout_seconds": 60,
    },
    "budget": {
        "max_retries_per_event": 2,
        "max_cost_per_event_usd": 5.0,
        "max_daily_cost_usd": 20.0,
        "max_weekly_cost_usd": 50.0,
    },
    "circuit_breaker": {
        "classification_accuracy_floor": 0.70,
        "recovery_threshold": 0.80,
        "evaluation_window_days": 7,
        "min_resolved_for_evaluation": 5,
    },
    "triage": {
        "scan_window_hours": 2,
        "max_events_per_cycle": 50,
        "fast_path_similarity_threshold": 0.7,
        "needs_llm_similarity_floor": 0.4,
        "transient_recurrence_threshold": 3,
        "transient_recurrence_window_hours": 24,
    },
    "escalation": {
        "channels": ["stdout"],
        "discord_webhook_url": "",
        "slack_webhook_url": "",
        "webhook_url": "",
        "fallback_channel": "stdout",
    },
    "remediation": {
        "handler": "subprocess",
        "command_template": "",
        "timeout_seconds": 300,
        "agent_timeout_seconds": 300,
        "skip_patterns": ["antml:thinking", "antml:.*artifact"],
        "circuit_breaker_max_failures": 3,
        "circuit_breaker_suspend_hours": 24,
        "max_daily_remediation_cost_usd": 10.0,
        "max_cost_per_attempt_usd": 2.0,
        "max_files_changed": 5,
        "max_lines_changed": 200,
        "recurrence_window_hours": 6,
    },
    "health": {
        "trend_pattern_spike_threshold": 3,
        "trend_remediation_failure_rate": 0.50,
        "trend_novel_rate": 0.40,
        "trend_override_rate": 0.30,
        "trend_daily_cost_warning_usd": 10.0,
        "trend_pending_drafts_max": 5,
    },
}


@dataclass
class Config:
    """OpenTriage configuration loaded from config.toml."""

    provider: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_CONFIG["provider"]))
    budget: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_CONFIG["budget"]))
    circuit_breaker: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_CONFIG["circuit_breaker"]))
    triage: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_CONFIG["triage"]))
    escalation: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_CONFIG["escalation"]))
    remediation: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_CONFIG["remediation"]))
    health: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_CONFIG["health"]))

    @classmethod
    def load(cls, path: Path) -> Config:
        """Load config from a TOML file, falling back to defaults for missing keys."""
        if not path.exists():
            return cls()
        raw = path.read_bytes()
        data = tomllib.loads(raw.decode())
        cfg = cls()
        for section in DEFAULT_CONFIG:
            if section in data:
                stored = getattr(cfg, section)
                stored.update(data[section])
        return cfg

    def save(self, path: Path) -> None:
        """Write current config to a TOML file."""
        data: dict[str, Any] = {
            "provider": dict(self.provider),
            "budget": dict(self.budget),
            "circuit_breaker": dict(self.circuit_breaker),
            "triage": dict(self.triage),
            "escalation": dict(self.escalation),
            "remediation": dict(self.remediation),
            "health": dict(self.health),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(tomli_w.dumps(data).encode())

    def get(self, dotted_key: str) -> Any:
        """Get a value by dotted key like 'provider.backend'."""
        parts = dotted_key.split(".", 1)
        section = getattr(self, parts[0], None)
        if section is None:
            raise KeyError(f"Unknown config section: {parts[0]}")
        if len(parts) == 1:
            return section
        return section[parts[1]]

    def set(self, dotted_key: str, value: str) -> None:
        """Set a value by dotted key. Coerces types based on defaults."""
        parts = dotted_key.split(".", 1)
        if len(parts) != 2:
            raise KeyError(f"Need section.key format, got: {dotted_key}")
        section = getattr(self, parts[0], None)
        if section is None:
            raise KeyError(f"Unknown config section: {parts[0]}")
        key = parts[1]
        default_val = DEFAULT_CONFIG.get(parts[0], {}).get(key)
        if isinstance(default_val, bool):
            value = value.lower() in ("true", "1", "yes")  # type: ignore[assignment]
        elif isinstance(default_val, int):
            value = int(value)  # type: ignore[assignment]
        elif isinstance(default_val, float):
            value = float(value)  # type: ignore[assignment]
        elif isinstance(default_val, list):
            value = [v.strip() for v in value.split(",")]  # type: ignore[assignment]
        section[key] = value


def resolve_paths(
    opentriage_dir: Path | None = None,
    openlog_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Resolve .opentriage/ and .openlog/ directory paths."""
    ot = opentriage_dir or Path(".opentriage")
    ol = openlog_dir or Path(".openlog")
    return ot.resolve(), ol.resolve()
