"""Tests for recurrence verification (F-AR06)."""

import json
import time
from pathlib import Path

from opentriage.io.writer import write_json
from opentriage.remediation.verification import (
    _count_active_minutes,
    add_pending_verification,
    check_recurrence,
    get_verification_summary,
)
from tests.conftest import write_events, write_fingerprints, write_state


def _make_state(ot_dir: Path, **overrides) -> dict:
    """Write a state.json with defaults."""
    state = {
        "circuit_breaker": "full-autonomy",
        "version": "1.0",
        "pending_verifications": [],
        "circuit_breakers": {},
    }
    state.update(overrides)
    write_state(ot_dir, state)
    return state


def test_add_pending_verification(tmp_dirs):
    ot_dir, _ = tmp_dirs
    _make_state(ot_dir)

    add_pending_verification(
        ot_dir,
        fingerprint_slug="selector-drift",
        attempt_id="rem-001",
        commit_sha="abc123",
    )

    state = json.loads((ot_dir / "state.json").read_text())
    pending = state["pending_verifications"]
    assert len(pending) == 1
    assert pending[0]["fingerprint_slug"] == "selector-drift"
    assert pending[0]["attempt_id"] == "rem-001"
    assert pending[0]["commit_sha"] == "abc123"
    assert pending[0]["status"] == "pending"
    assert pending[0]["recurrence_window_hours"] == 6


def test_add_pending_verification_no_duplicate(tmp_dirs):
    ot_dir, _ = tmp_dirs
    _make_state(ot_dir)

    add_pending_verification(ot_dir, "slug-a", "rem-001")
    add_pending_verification(ot_dir, "slug-a", "rem-001")  # duplicate

    state = json.loads((ot_dir / "state.json").read_text())
    assert len(state["pending_verifications"]) == 1


def test_check_recurrence_no_pending(tmp_dirs):
    ot_dir, ol_dir = tmp_dirs
    _make_state(ot_dir)
    results = check_recurrence(ot_dir, ol_dir)
    assert results == []


def test_check_recurrence_verified(tmp_dirs):
    """Fix applied, window expires, no recurrence → verified."""
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, [
        {"slug": "test-fix", "patterns": ["test error"], "status": "confirmed",
         "severity": "recoverable", "remedy": "fix it"},
    ])

    fixed_at = time.time() - 8 * 3600  # 8 hours ago
    state = {
        "circuit_breaker": "full-autonomy",
        "version": "1.0",
        "pending_verifications": [{
            "fingerprint_slug": "test-fix",
            "fixed_at_ts": fixed_at,
            "attempt_id": "rem-verified",
            "commit_sha": "abc123",
            "recurrence_window_hours": 6,
            "active_minutes_post_fix": 0,
            "status": "pending",
        }],
        "circuit_breakers": {},
    }
    write_state(ot_dir, state)

    # Create enough post-fix events to reach 60 active minutes
    events = []
    for i in range(20):
        events.append({
            "ts": fixed_at + (i * 400),  # Every ~6.5 minutes
            "kind": "error",
            "ref": f"post-{i}",
            "session_id": "sess-post",
            "f_raw": "unrelated error pattern",
        })
    write_events(ol_dir, events, session="sess-post")

    results = check_recurrence(ot_dir, ol_dir)
    assert len(results) == 1
    assert results[0]["status"] == "verified"
    assert results[0]["fingerprint_slug"] == "test-fix"


def test_check_recurrence_recurred(tmp_dirs):
    """Fix applied, same fingerprint recurs → recurred."""
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, [
        {"slug": "recur-test", "patterns": ["recurring error"], "status": "confirmed",
         "severity": "recoverable", "remedy": "fix it"},
    ])

    fixed_at = time.time() - 3600  # 1 hour ago

    state = {
        "circuit_breaker": "full-autonomy",
        "version": "1.0",
        "pending_verifications": [{
            "fingerprint_slug": "recur-test",
            "fixed_at_ts": fixed_at,
            "attempt_id": "rem-recur",
            "commit_sha": "def456",
            "recurrence_window_hours": 6,
            "active_minutes_post_fix": 0,
            "status": "pending",
        }],
        "circuit_breakers": {},
    }
    write_state(ot_dir, state)

    # Write a post-fix correlation matching the same fingerprint
    from opentriage.io.writer import append_jsonl
    from datetime import datetime, timezone
    corr_record = {
        "ts": fixed_at + 1800,  # 30 min after fix
        "ref": "post-fix-event",
        "session_id": "sess-post",
        "matched_fingerprint": "recur-test",
        "classification": "known-pattern",
        "confidence": "high",
    }
    date_str = datetime.fromtimestamp(corr_record["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
    append_jsonl(ot_dir / "correlations" / f"{date_str}.jsonl", corr_record)

    results = check_recurrence(ot_dir, ol_dir)
    assert len(results) == 1
    assert results[0]["status"] == "recurred"
    assert results[0]["fingerprint_slug"] == "recur-test"


def test_check_recurrence_extends_window(tmp_dirs):
    """Window expired but not enough active time → extends window."""
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, [])

    fixed_at = time.time() - 8 * 3600  # 8 hours ago
    state = {
        "circuit_breaker": "full-autonomy",
        "version": "1.0",
        "pending_verifications": [{
            "fingerprint_slug": "quiet-fix",
            "fixed_at_ts": fixed_at,
            "attempt_id": "rem-quiet",
            "commit_sha": "ghi789",
            "recurrence_window_hours": 6,
            "active_minutes_post_fix": 0,
            "status": "pending",
        }],
        "circuit_breakers": {},
    }
    write_state(ot_dir, state)

    # No post-fix events → 0 active minutes → window extended
    results = check_recurrence(ot_dir, ol_dir)
    assert results == []  # No results because it was extended, not resolved

    # Check state was updated with extended window
    new_state = json.loads((ot_dir / "state.json").read_text())
    pending = new_state["pending_verifications"]
    assert len(pending) == 1
    assert pending[0]["recurrence_window_hours"] == 12  # Extended from 6 to 12


def test_check_recurrence_still_pending(tmp_dirs):
    """Within window, no recurrence → stays pending."""
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, [])

    fixed_at = time.time() - 1800  # 30 minutes ago (within 6h window)
    state = {
        "circuit_breaker": "full-autonomy",
        "version": "1.0",
        "pending_verifications": [{
            "fingerprint_slug": "still-watching",
            "fixed_at_ts": fixed_at,
            "attempt_id": "rem-watching",
            "commit_sha": "jkl012",
            "recurrence_window_hours": 6,
            "active_minutes_post_fix": 0,
            "status": "pending",
        }],
        "circuit_breakers": {},
    }
    write_state(ot_dir, state)

    results = check_recurrence(ot_dir, ol_dir)
    assert results == []  # No status change yet

    new_state = json.loads((ot_dir / "state.json").read_text())
    assert len(new_state["pending_verifications"]) == 1
    assert new_state["pending_verifications"][0]["status"] == "pending"


def test_count_active_minutes():
    now = time.time()
    # Events every 4 minutes for 60 minutes → 12 5-minute buckets
    events = [{"ts": now + i * 240} for i in range(15)]
    minutes = _count_active_minutes(events, now - 1)
    assert minutes >= 50  # At least 10 buckets * 5 min


def test_count_active_minutes_empty():
    assert _count_active_minutes([], time.time()) == 0


def test_count_active_minutes_sparse():
    now = time.time()
    # Only 2 events far apart → 2 buckets
    events = [
        {"ts": now + 100},
        {"ts": now + 5000},
    ]
    minutes = _count_active_minutes(events, now)
    assert minutes == 10  # 2 buckets * 5


def test_get_verification_summary(tmp_dirs):
    ot_dir, _ = tmp_dirs
    state = {
        "circuit_breaker": "full-autonomy",
        "version": "1.0",
        "pending_verifications": [
            {"status": "pending", "fingerprint_slug": "a"},
            {"status": "pending", "fingerprint_slug": "b"},
            {"status": "verified", "fingerprint_slug": "c"},
            {"status": "recurred", "fingerprint_slug": "d"},
        ],
    }
    write_state(ot_dir, state)

    summary = get_verification_summary(ot_dir)
    assert summary["pending_count"] == 2
    assert summary["verified_count"] == 1
    assert summary["recurred_count"] == 1
