"""Tests for health monitor (F-OT07)."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from opentriage.config import Config
from opentriage.health.monitor import run_health
from opentriage.health.trends import detect_trends
from opentriage.io.writer import write_correlation, write_remediation, write_metrics
from tests.conftest import write_state


def test_health_no_data(tmp_dirs):
    ot_dir, _ = tmp_dirs
    write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0",
                         "demotion_history": []})
    cfg = Config()
    result = run_health(cfg, ot_dir, days=1, today_only=True)
    assert result["total_events"] == 0
    assert result["total_cost_usd"] == 0


def test_health_computes_metrics(tmp_dirs):
    ot_dir, _ = tmp_dirs
    write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0",
                         "demotion_history": []})
    now = time.time()

    # Write some correlations
    for i in range(5):
        write_correlation(ot_dir, {
            "ts": now - (i * 60),
            "ref": f"t{i}",
            "session_id": f"s{i}",
            "classification": "known-pattern",
            "matched_fingerprint": "test-slug",
            "confidence": "high",
            "tier": "fast_path",
            "method": "substring",
        })

    # Write some remediations
    for i in range(3):
        write_remediation(ot_dir, {
            "ts": now - (i * 60),
            "event_ref": f"t{i}",
            "session_id": f"s{i}",
            "fingerprint_slug": "test-slug",
            "outcome": "success" if i < 2 else "failure",
            "estimated_cost_usd": 0.15,
        })

    cfg = Config()
    result = run_health(cfg, ot_dir, days=1, today_only=True)
    assert result["total_events"] == 5
    assert result["total_remediations"] == 3
    assert result["total_successes"] == 2

    # Check daily metrics file was written
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    metrics_path = ot_dir / "metrics" / f"{today}.json"
    assert metrics_path.exists()
    metrics = json.loads(metrics_path.read_text())
    assert metrics["remediations"]["success_rate"] is not None


def test_trend_pattern_spike(tmp_dirs):
    ot_dir, _ = tmp_dirs
    write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0",
                         "demotion_history": []})
    now = time.time()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Write spike: 5 events for same slug today
    for i in range(5):
        write_correlation(ot_dir, {
            "ts": now - (i * 10),
            "ref": f"t{i}",
            "session_id": f"s{i}",
            "classification": "known-pattern",
            "matched_fingerprint": "spiking-slug",
            "confidence": "high",
            "tier": "fast_path",
            "method": "substring",
        })

    # Write historical (3+ days with 0-1 per day)
    from datetime import timedelta
    for day_offset in range(1, 5):
        date_str = (datetime.now(timezone.utc) - timedelta(days=day_offset)).strftime("%Y-%m-%d")
        write_metrics(ot_dir, date_str, {
            "date": date_str,
            "events": {"total_scanned": 2, "errors_found": 2, "correlated": 2, "uncorrelated_remaining": 0},
            "classifications": {"known_pattern_fast_path": 1, "known_pattern_llm": 0, "novel": 0,
                               "transient": 0, "deferred": 0, "override_count": 0, "override_rate": 0},
            "remediations": {"attempted": 0, "succeeded": 0, "failed": 0, "no_result": 0,
                            "escalated_budget": 0, "success_rate": None},
            "cost": {"cheap_tier_usd": 0, "standard_tier_usd": 0, "expensive_tier_usd": 0,
                    "remediation_subprocess_usd": 0, "total_usd": 0},
            "system": {"circuit_breaker_state": "full-autonomy", "state_transitions": 0,
                      "pending_drafts": 0, "triage_cycles_run": 0, "escalations_sent": 0},
        })

    today_metrics = {
        "events": {"total_scanned": 5, "errors_found": 5, "correlated": 5, "uncorrelated_remaining": 0},
        "classifications": {"known_pattern_fast_path": 5, "known_pattern_llm": 0, "novel": 0,
                           "transient": 0, "deferred": 0, "override_count": 0, "override_rate": 0},
        "remediations": {"attempted": 0, "succeeded": 0, "failed": 0, "no_result": 0,
                        "escalated_budget": 0, "success_rate": None},
        "cost": {"total_usd": 0},
        "system": {"pending_drafts": 0},
    }

    cfg = Config()
    alerts = detect_trends(cfg, ot_dir, today_metrics)
    spike_alerts = [a for a in alerts if "spike" in a.get("title", "").lower()]
    assert len(spike_alerts) >= 1


def test_trend_pending_drafts(tmp_dirs):
    ot_dir, _ = tmp_dirs
    write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0",
                         "demotion_history": []})

    # Write enough historical metrics
    from datetime import timedelta
    for day_offset in range(1, 5):
        date_str = (datetime.now(timezone.utc) - timedelta(days=day_offset)).strftime("%Y-%m-%d")
        write_metrics(ot_dir, date_str, {"date": date_str})

    today_metrics = {
        "events": {"errors_found": 10},
        "classifications": {"novel": 0, "override_rate": 0},
        "cost": {"total_usd": 0},
        "system": {"pending_drafts": 10},
        "remediations": {},
    }

    cfg = Config()
    alerts = detect_trends(cfg, ot_dir, today_metrics)
    draft_alerts = [a for a in alerts if "draft" in a.get("title", "").lower()]
    assert len(draft_alerts) >= 1
