"""Shared test fixtures."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def tmp_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Create .opentriage/ and .openlog/ test directories."""
    ot = tmp_path / ".opentriage"
    ol = tmp_path / ".openlog"
    ot.mkdir()
    ol.mkdir()
    for sub in ("correlations", "remediations", "drafts", "metrics"):
        (ot / sub).mkdir()
    (ol / "events").mkdir()
    return ot, ol


@pytest.fixture
def sample_state() -> dict[str, Any]:
    """Default circuit breaker state."""
    return {
        "circuit_breaker": "full-autonomy",
        "last_triage_run": None,
        "last_health_run": None,
        "demotion_history": [],
        "rolling_remediation_success_rate": None,
        "rolling_override_rate": None,
        "net_remediation_effect": None,
        "total_remediations": 0,
        "total_escalations": 0,
        "consecutive_provider_errors": 0,
        "human_approved_promotion": False,
        "version": "1.0",
    }


@pytest.fixture
def sample_fingerprints() -> list[dict[str, Any]]:
    """Sample fingerprint registry."""
    return [
        {
            "slug": "circular-import",
            "patterns": ["circular import", "circular import between"],
            "count": 10,
            "status": "confirmed",
            "severity": "recoverable",
            "remedy": "Check barrel files, split shared types into types.ts",
        },
        {
            "slug": "confabulation",
            "patterns": ["claimed completion but modified 0 files", "confabulation detected"],
            "count": 5,
            "status": "confirmed",
            "severity": "fatal",
            "remedy": "Review task prompt for ambiguous success criteria",
        },
        {
            "slug": "provisional-pattern",
            "patterns": ["some provisional thing"],
            "count": 1,
            "status": "provisional",
            "severity": None,
            "remedy": None,
        },
    ]


@pytest.fixture
def sample_events() -> list[dict[str, Any]]:
    """Sample error events."""
    now = time.time()
    return [
        {
            "ts": now - 100,
            "kind": "error",
            "ref": "task-1",
            "session_id": "sess-001",
            "f_raw": "circular import between auth and user",
            "stderr": "ImportError: circular import detected",
            "exit_code": 1,
        },
        {
            "ts": now - 80,
            "kind": "error",
            "ref": "task-2",
            "session_id": "sess-001",
            "f_raw": "widget factory explosion in module X",
            "stderr": "RuntimeError: widget factory failed",
            "exit_code": 1,
        },
        {
            "ts": now - 60,
            "kind": "error",
            "ref": "task-3",
            "session_id": "sess-002",
            "f_raw": "claimed completion but modified 0 files",
            "stderr": "",
            "exit_code": 0,
        },
    ]


def write_fingerprints(ol_dir: Path, fingerprints: list[dict]) -> None:
    """Write fingerprints.json to openlog dir."""
    (ol_dir / "fingerprints.json").write_text(json.dumps(fingerprints, indent=2))


def write_events(ol_dir: Path, events: list[dict], session: str = "2026-04-04-sess") -> None:
    """Write events to a JSONL file in openlog dir."""
    path = ol_dir / "events" / f"{session}.jsonl"
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def write_state(ot_dir: Path, state: dict) -> None:
    """Write state.json."""
    (ot_dir / "state.json").write_text(json.dumps(state, indent=2))


class MockProvider:
    """Mock LLM provider for testing."""

    def __init__(self, responses: dict[str, str] | None = None):
        self.calls: list[tuple[str, list[dict]]] = []
        self._responses = responses or {}
        self._default_response = json.dumps({
            "classification": "novel",
            "matched_fingerprint": None,
            "confidence": "high",
            "reasoning": "Unknown pattern not in registry",
        })

    def complete(self, messages: list[dict], tier: str = "cheap") -> str:
        self.calls.append((tier, messages))
        return self._responses.get(tier, self._default_response)

    def estimate_cost(self, input_tokens: int, output_tokens: int, tier: str) -> float:
        return 0.01
