"""Tests for auto-remediation engine (F-OT04)."""

import json
import time
from pathlib import Path

from opentriage.config import Config
from opentriage.remediation.budget import check_budget
from opentriage.remediation.engine import run_remediation
from opentriage.remediation.handlers import build_remedy_context, execute_noop
from tests.conftest import write_fingerprints, write_state


def test_budget_check_passes(tmp_dirs):
    ot_dir, _ = tmp_dirs
    event = {"ref": "t1", "session_id": "s1"}
    ok, reason = check_budget(event, Config().budget, ot_dir)
    assert ok is True
    assert reason == ""


def test_budget_check_max_retries(tmp_dirs):
    ot_dir, _ = tmp_dirs
    # Write 2 existing remediations for same event
    from opentriage.io.writer import write_remediation
    for i in range(2):
        write_remediation(ot_dir, {
            "ts": time.time(),
            "event_ref": "t1",
            "session_id": "s1",
            "estimated_cost_usd": 0.10,
        })

    event = {"ref": "t1", "session_id": "s1"}
    ok, reason = check_budget(event, {"max_retries_per_event": 2, "max_cost_per_event_usd": 5.0,
                                       "max_daily_cost_usd": 20.0, "max_weekly_cost_usd": 50.0}, ot_dir)
    assert ok is False
    assert "max_retries_per_event" in reason


def test_budget_check_daily_cost(tmp_dirs):
    ot_dir, _ = tmp_dirs
    from opentriage.io.writer import write_remediation
    # Write remediation that costs $25
    write_remediation(ot_dir, {
        "ts": time.time(),
        "event_ref": "other",
        "session_id": "other",
        "estimated_cost_usd": 25.0,
    })

    event = {"ref": "t1", "session_id": "s1"}
    ok, reason = check_budget(event, {"max_retries_per_event": 2, "max_cost_per_event_usd": 5.0,
                                       "max_daily_cost_usd": 20.0, "max_weekly_cost_usd": 50.0}, ot_dir)
    assert ok is False
    assert "max_daily_cost_usd" in reason


def test_noop_handler():
    event = {"ref": "t1", "f_raw": "test error"}
    fp = {"slug": "test-slug", "remedy": "fix it"}
    exit_code, output = execute_noop(event, fp, "remedy context")
    assert exit_code == 0
    assert output == "noop"


def test_build_remedy_context():
    event = {"f_raw": "circular import", "stderr": "ImportError", "ref": "t1"}
    fp = {"slug": "circular-import", "remedy": "Split shared types"}
    ctx = build_remedy_context(event, fp)
    assert "Split shared types" in ctx
    assert "circular import" in ctx
    assert "circular-import" in ctx


def test_run_remediation_with_noop(tmp_dirs, sample_fingerprints):
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, sample_fingerprints)
    write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0"})

    cfg = Config()
    cfg.remediation["handler"] = "noop"

    correlations = [
        {
            "ts": time.time(),
            "ref": "t1",
            "session_id": "s1",
            "classification": "known-pattern",
            "matched_fingerprint": "circular-import",
            "confidence": "high",
        },
    ]

    rems = run_remediation(correlations, cfg, ot_dir, ol_dir)
    assert len(rems) == 1
    assert rems[0]["fingerprint_slug"] == "circular-import"
    assert rems[0]["handler_exit_code"] == 0


def test_run_remediation_no_remedy_skips(tmp_dirs):
    """Fingerprint with no remedy should not trigger remediation."""
    ot_dir, ol_dir = tmp_dirs
    fps = [{"slug": "no-remedy", "patterns": ["no remedy"], "status": "confirmed", "severity": None, "remedy": None}]
    write_fingerprints(ol_dir, fps)
    write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0"})

    cfg = Config()
    correlations = [
        {"ts": time.time(), "ref": "t1", "session_id": "s1",
         "classification": "known-pattern", "matched_fingerprint": "no-remedy", "confidence": "high"},
    ]

    rems = run_remediation(correlations, cfg, ot_dir, ol_dir)
    assert len(rems) == 0


def test_run_remediation_empty_remedy_skips(tmp_dirs):
    """Empty remedy string treated as null."""
    ot_dir, ol_dir = tmp_dirs
    fps = [{"slug": "empty-rem", "patterns": ["err"], "status": "confirmed", "severity": None, "remedy": ""}]
    write_fingerprints(ol_dir, fps)
    write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0"})

    cfg = Config()
    correlations = [
        {"ts": time.time(), "ref": "t1", "session_id": "s1",
         "classification": "known-pattern", "matched_fingerprint": "empty-rem", "confidence": "high"},
    ]

    rems = run_remediation(correlations, cfg, ot_dir, ol_dir)
    assert len(rems) == 0


def test_budget_exceeded_records(tmp_dirs, sample_fingerprints):
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, sample_fingerprints)
    write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0"})

    cfg = Config()
    cfg.budget["max_retries_per_event"] = 0  # No retries allowed

    correlations = [
        {"ts": time.time(), "ref": "t1", "session_id": "s1",
         "classification": "known-pattern", "matched_fingerprint": "circular-import", "confidence": "high"},
    ]

    rems = run_remediation(correlations, cfg, ot_dir, ol_dir)
    assert len(rems) == 1
    assert rems[0]["outcome"] == "budget_exceeded"
