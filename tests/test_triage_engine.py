"""Tests for triage engine (F-OT02)."""

import json
import time
from pathlib import Path

from opentriage.config import Config
from opentriage.triage.engine import run_triage
from tests.conftest import MockProvider, write_events, write_fingerprints, write_state


def test_fast_path_known_pattern(tmp_dirs, sample_fingerprints, sample_events):
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, sample_fingerprints)
    write_events(ol_dir, sample_events)
    state = {
        "circuit_breaker": "full-autonomy",
        "last_triage_run": None, "last_health_run": None,
        "demotion_history": [], "rolling_remediation_success_rate": None,
        "rolling_override_rate": None, "net_remediation_effect": None,
        "total_remediations": 0, "total_escalations": 0,
        "consecutive_provider_errors": 0, "human_approved_promotion": False,
        "version": "1.0",
    }
    write_state(ot_dir, state)

    cfg = Config()
    result = run_triage(cfg, None, ot_dir, ol_dir, scan_all=True, dry_run=True)

    assert result["status"] == "ok"
    stats = result["stats"]
    # "circular import" and "confabulation" should fast-path match
    assert stats["fast_path"] >= 2


def test_fast_path_no_llm_call(tmp_dirs, sample_fingerprints):
    """Fast-path classified events should NOT call the LLM."""
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, sample_fingerprints)
    now = time.time()
    events = [
        {"ts": now - 10, "kind": "error", "ref": "t1", "session_id": "s1",
         "f_raw": "circular import between auth and user", "stderr": "", "exit_code": 1},
    ]
    write_events(ol_dir, events)
    write_state(ot_dir, {
        "circuit_breaker": "full-autonomy", "last_triage_run": None,
        "last_health_run": None, "demotion_history": [],
        "rolling_remediation_success_rate": None, "rolling_override_rate": None,
        "net_remediation_effect": None, "total_remediations": 0,
        "total_escalations": 0, "consecutive_provider_errors": 0,
        "human_approved_promotion": False, "version": "1.0",
    })

    provider = MockProvider()
    cfg = Config()
    result = run_triage(cfg, provider, ot_dir, ol_dir, scan_all=True, dry_run=True)

    assert result["stats"]["fast_path"] == 1
    assert len(provider.calls) == 0  # No LLM calls for fast-path


def test_slow_path_novel(tmp_dirs, sample_fingerprints):
    """Unknown event triggers LLM classification."""
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, sample_fingerprints)
    now = time.time()
    events = [
        {"ts": now - 10, "kind": "error", "ref": "t1", "session_id": "s1",
         "f_raw": "widget factory explosion in module X", "stderr": "", "exit_code": 1},
    ]
    write_events(ol_dir, events)
    write_state(ot_dir, {
        "circuit_breaker": "full-autonomy", "last_triage_run": None,
        "last_health_run": None, "demotion_history": [],
        "rolling_remediation_success_rate": None, "rolling_override_rate": None,
        "net_remediation_effect": None, "total_remediations": 0,
        "total_escalations": 0, "consecutive_provider_errors": 0,
        "human_approved_promotion": False, "version": "1.0",
    })

    provider = MockProvider()
    cfg = Config()
    result = run_triage(cfg, provider, ot_dir, ol_dir, scan_all=True)

    assert result["status"] == "ok"
    # Provider should have been called
    assert len(provider.calls) >= 1


def test_suspended_skips_triage(tmp_dirs, sample_fingerprints, sample_events):
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, sample_fingerprints)
    write_events(ol_dir, sample_events)
    write_state(ot_dir, {"circuit_breaker": "suspended", "version": "1.0"})

    cfg = Config()
    result = run_triage(cfg, None, ot_dir, ol_dir, scan_all=True)
    assert result["status"] == "skipped"
    assert result["events_processed"] == 0


def test_no_events_dir(tmp_dirs):
    ot_dir, ol_dir = tmp_dirs
    # Remove events dir
    import shutil
    events_dir = ol_dir / "events"
    if events_dir.exists():
        shutil.rmtree(events_dir)
    write_state(ot_dir, {
        "circuit_breaker": "full-autonomy", "version": "1.0",
        "last_triage_run": None, "last_health_run": None,
        "demotion_history": [], "rolling_remediation_success_rate": None,
        "rolling_override_rate": None, "net_remediation_effect": None,
        "total_remediations": 0, "total_escalations": 0,
        "consecutive_provider_errors": 0, "human_approved_promotion": False,
    })

    cfg = Config()
    result = run_triage(cfg, None, ot_dir, ol_dir)
    assert result["status"] == "ok"
    assert result["events_processed"] == 0


def test_no_fingerprints_all_go_slow_path(tmp_dirs):
    """Missing fingerprints.json means all events go to slow path."""
    ot_dir, ol_dir = tmp_dirs
    now = time.time()
    events = [
        {"ts": now - 10, "kind": "error", "ref": "t1", "session_id": "s1",
         "f_raw": "something broke", "stderr": "", "exit_code": 1},
    ]
    write_events(ol_dir, events)
    write_state(ot_dir, {
        "circuit_breaker": "full-autonomy", "last_triage_run": None,
        "last_health_run": None, "demotion_history": [],
        "rolling_remediation_success_rate": None, "rolling_override_rate": None,
        "net_remediation_effect": None, "total_remediations": 0,
        "total_escalations": 0, "consecutive_provider_errors": 0,
        "human_approved_promotion": False, "version": "1.0",
    })

    provider = MockProvider()
    cfg = Config()
    result = run_triage(cfg, provider, ot_dir, ol_dir, scan_all=True)
    assert result["stats"]["fast_path"] == 0
    assert len(provider.calls) >= 1


def test_duplicate_events_not_reclassified(tmp_dirs, sample_fingerprints):
    """Already-correlated events should be skipped."""
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, sample_fingerprints)
    now = time.time()
    events = [
        {"ts": now - 10, "kind": "error", "ref": "t1", "session_id": "s1",
         "f_raw": "circular import between auth and user", "stderr": "", "exit_code": 1},
    ]
    write_events(ol_dir, events)
    write_state(ot_dir, {
        "circuit_breaker": "full-autonomy", "last_triage_run": None,
        "last_health_run": None, "demotion_history": [],
        "rolling_remediation_success_rate": None, "rolling_override_rate": None,
        "net_remediation_effect": None, "total_remediations": 0,
        "total_escalations": 0, "consecutive_provider_errors": 0,
        "human_approved_promotion": False, "version": "1.0",
    })

    cfg = Config()
    # First run
    run_triage(cfg, None, ot_dir, ol_dir, scan_all=True)
    # Second run — same events should be skipped
    result = run_triage(cfg, None, ot_dir, ol_dir, scan_all=True)
    assert result["events_processed"] == 0


def test_transient_recurrence_detection(tmp_dirs, sample_fingerprints):
    """3+ similar transient events should reclassify as novel."""
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, sample_fingerprints)
    now = time.time()

    # Write 3 transient correlations with similar f_raw
    from opentriage.io.writer import write_correlation
    for i in range(3):
        write_correlation(ot_dir, {
            "ts": now - (100 * i),
            "ref": f"t{i}",
            "session_id": f"s{i}",
            "f_raw": f"widget factory error in module {i}",
            "classification": "transient",
            "matched_fingerprint": None,
            "confidence": "medium",
            "tier": "slow_path",
            "method": "llm_cheap",
        })

    write_state(ot_dir, {
        "circuit_breaker": "full-autonomy", "last_triage_run": None,
        "last_health_run": None, "demotion_history": [],
        "rolling_remediation_success_rate": None, "rolling_override_rate": None,
        "net_remediation_effect": None, "total_remediations": 0,
        "total_escalations": 0, "consecutive_provider_errors": 0,
        "human_approved_promotion": False, "version": "1.0",
    })

    # Write a new event to trigger triage
    events = [
        {"ts": now, "kind": "error", "ref": "t-new", "session_id": "s-new",
         "f_raw": "totally different error", "stderr": "", "exit_code": 1},
    ]
    write_events(ol_dir, events)

    cfg = Config()
    cfg.triage["transient_recurrence_threshold"] = 3
    provider = MockProvider()
    result = run_triage(cfg, provider, ot_dir, ol_dir, scan_all=True)

    # Check that a novel correlation was added from recurrence detection
    from opentriage.io.reader import load_correlations
    all_corrs = load_correlations(ot_dir)
    novel_from_recurrence = [
        c for c in all_corrs
        if c.get("tier") == "recurrence_detection"
    ]
    assert len(novel_from_recurrence) >= 1
