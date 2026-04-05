"""Tests for management CLI commands (drafts, approve, reject, escalations, validate, calibrate, revert, cleanup)."""

import json
import os
import time
from pathlib import Path

import pytest

from opentriage.cli import main


# --- Helpers ---

def _init(tmp_path, monkeypatch):
    """Initialize opentriage + openlog in tmp_path."""
    monkeypatch.chdir(tmp_path)
    main(["init"])
    ol = tmp_path / ".openlog"
    ol.mkdir(exist_ok=True)
    (ol / "events").mkdir(exist_ok=True)
    return tmp_path / ".opentriage", ol


def _write_draft(ot_dir, slug, **overrides):
    """Write a draft file and return the data."""
    data = {
        "slug": slug,
        "description": f"Test draft: {slug}",
        "patterns": [f"error pattern for {slug}"],
        "severity": "recoverable",
        "category": "test",
        "remedy": "Fix it",
        "root_cause_hypothesis": "HYPOTHESIS",
        "source_event": {"session_id": "sess-001", "ref": "task-1", "ts": time.time()},
        "status": "proposed",
        "created": "2026-04-01",
        "recurrence_count": 2,
    }
    data.update(overrides)
    path = ot_dir / "drafts" / f"{slug}.json"
    path.write_text(json.dumps(data, indent=2))
    return data


# --- drafts ---

def test_drafts_empty(tmp_path, monkeypatch, capsys):
    ot_dir, _ = _init(tmp_path, monkeypatch)
    main(["drafts"])
    captured = capsys.readouterr()
    assert "No pending drafts" in captured.out


def test_drafts_lists(tmp_path, monkeypatch, capsys):
    ot_dir, _ = _init(tmp_path, monkeypatch)
    _write_draft(ot_dir, "test-pattern-a")
    _write_draft(ot_dir, "test-pattern-b")
    main(["drafts"])
    captured = capsys.readouterr()
    assert "Pending Drafts (2)" in captured.out
    assert "test-pattern-a" in captured.out
    assert "test-pattern-b" in captured.out


def test_drafts_json(tmp_path, monkeypatch, capsys):
    ot_dir, _ = _init(tmp_path, monkeypatch)
    _write_draft(ot_dir, "my-draft")
    capsys.readouterr()  # clear init output
    main(["drafts", "--json"])
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data) == 1
    assert data[0]["slug"] == "my-draft"


# --- approve ---

def test_approve_success(tmp_path, monkeypatch, capsys):
    ot_dir, ol_dir = _init(tmp_path, monkeypatch)
    _write_draft(ot_dir, "good-pattern")

    main(["approve", "good-pattern", "--comment", "Looks correct"])
    captured = capsys.readouterr()
    assert "Approved: good-pattern" in captured.out

    # Draft moved to approved/
    assert not (ot_dir / "drafts" / "good-pattern.json").exists()
    approved = ot_dir / "drafts" / "approved" / "good-pattern.json"
    assert approved.exists()
    approved_data = json.loads(approved.read_text())
    assert "approved_at" in approved_data
    assert approved_data["approval_comment"] == "Looks correct"

    # Fingerprint added to registry
    fp_data = json.loads((ol_dir / "fingerprints.json").read_text())
    slugs = [fp["slug"] for fp in fp_data]
    assert "good-pattern" in slugs


def test_approve_missing_slug(tmp_path, monkeypatch, capsys):
    _init(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main(["approve", "nonexistent"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "not found" in captured.err.lower()


def test_approve_missing_required_fields(tmp_path, monkeypatch, capsys):
    ot_dir, _ = _init(tmp_path, monkeypatch)
    _write_draft(ot_dir, "bad-draft", severity=None, patterns=None)
    with pytest.raises(SystemExit) as exc:
        main(["approve", "bad-draft"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "missing required" in captured.err.lower()


def test_approve_appends_to_existing_registry(tmp_path, monkeypatch, capsys):
    ot_dir, ol_dir = _init(tmp_path, monkeypatch)
    # Pre-existing fingerprints
    existing = [{"slug": "existing-one", "patterns": ["x"], "severity": "fatal"}]
    (ol_dir / "fingerprints.json").write_text(json.dumps(existing))

    _write_draft(ot_dir, "new-pattern")
    main(["approve", "new-pattern"])

    fp_data = json.loads((ol_dir / "fingerprints.json").read_text())
    assert len(fp_data) == 2
    assert fp_data[0]["slug"] == "existing-one"
    assert fp_data[1]["slug"] == "new-pattern"


# --- reject ---

def test_reject_success(tmp_path, monkeypatch, capsys):
    ot_dir, _ = _init(tmp_path, monkeypatch)
    _write_draft(ot_dir, "bad-pattern")

    main(["reject", "bad-pattern", "--reason", "False positive"])
    captured = capsys.readouterr()
    assert "Rejected: bad-pattern" in captured.out

    # Draft moved
    assert not (ot_dir / "drafts" / "bad-pattern.json").exists()
    rejected = ot_dir / "drafts" / "rejected" / "bad-pattern.json"
    assert rejected.exists()
    data = json.loads(rejected.read_text())
    assert data["rejected_reason"] == "False positive"
    assert "rejected_at" in data


def test_reject_missing_slug(tmp_path, monkeypatch, capsys):
    _init(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main(["reject", "nonexistent"])
    assert exc.value.code == 1


# --- escalations ---

def test_escalations_empty(tmp_path, monkeypatch, capsys):
    _init(tmp_path, monkeypatch)
    main(["escalations"])
    captured = capsys.readouterr()
    assert "No escalations" in captured.out


def test_escalations_display(tmp_path, monkeypatch, capsys):
    ot_dir, _ = _init(tmp_path, monkeypatch)
    # Write some escalation records
    esc_path = ot_dir / "escalations.jsonl"
    for i in range(5):
        record = {
            "ts": time.time() - (5 - i) * 60,
            "severity": "high",
            "type": "novel_pattern",
            "title": f"Escalation {i}",
            "channel": "stdout",
            "delivery_status": "sent",
        }
        with open(esc_path, "a") as f:
            f.write(json.dumps(record) + "\n")

    main(["escalations", "--last", "3"])
    captured = capsys.readouterr()
    assert "last 3" in captured.out.lower()
    assert "Escalation 2" in captured.out
    assert "Escalation 4" in captured.out


def test_escalations_json(tmp_path, monkeypatch, capsys):
    ot_dir, _ = _init(tmp_path, monkeypatch)
    esc_path = ot_dir / "escalations.jsonl"
    record = {"ts": time.time(), "severity": "high", "type": "test", "title": "Test esc"}
    with open(esc_path, "w") as f:
        f.write(json.dumps(record) + "\n")

    capsys.readouterr()  # clear init output
    main(["escalations", "--json"])
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["type"] == "test"


# --- validate ---

def test_validate_all_pass(tmp_path, monkeypatch, capsys):
    ot_dir, ol_dir = _init(tmp_path, monkeypatch)
    # Ensure openlog has events
    (ol_dir / "events" / "2026-04-04.jsonl").write_text("")
    # Set API key env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    try:
        main(["validate"])
    except SystemExit as e:
        assert e.code == 0
    captured = capsys.readouterr()
    assert "\u2705" in captured.out


def test_validate_missing_init(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    try:
        main(["validate"])
    except SystemExit as e:
        assert e.code == 1
    captured = capsys.readouterr()
    assert "\u274c" in captured.out


def test_validate_missing_api_key(tmp_path, monkeypatch, capsys):
    ot_dir, ol_dir = _init(tmp_path, monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    try:
        main(["validate"])
    except SystemExit as e:
        assert e.code == 1
    captured = capsys.readouterr()
    assert "ANTHROPIC_API_KEY" in captured.out


# --- calibrate ---

def test_calibrate_no_data(tmp_path, monkeypatch, capsys):
    _init(tmp_path, monkeypatch)
    main(["calibrate"])
    captured = capsys.readouterr()
    assert "No events" in captured.out


def test_calibrate_with_data(tmp_path, monkeypatch, capsys):
    ot_dir, ol_dir = _init(tmp_path, monkeypatch)

    # Write fingerprints
    fps = [{"slug": "known-error", "patterns": ["known"], "status": "confirmed"}]
    (ol_dir / "fingerprints.json").write_text(json.dumps(fps))

    # Write correlations with both classification and fingerprint match
    corr_dir = ot_dir / "correlations"
    records = [
        {"classification": "known", "matched_fingerprint": "known-error", "ts": time.time()},
        {"classification": "known", "matched_fingerprint": "known-error", "ts": time.time()},
        {"classification": "novel", "matched_fingerprint": "unknown-thing", "ts": time.time()},
    ]
    with open(corr_dir / "2026-04-04.jsonl", "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    main(["calibrate", "--events", "10"])
    captured = capsys.readouterr()
    assert "Calibration Report" in captured.out
    assert "Agreement rate" in captured.out


# --- revert ---

def test_revert_success(tmp_path, monkeypatch, capsys):
    ot_dir, _ = _init(tmp_path, monkeypatch)
    rem_dir = ot_dir / "remediations"

    records = [
        {"id": "rem-001", "fingerprint_slug": "test-error", "outcome": "success", "ts": time.time()},
        {"id": "rem-002", "fingerprint_slug": "other-error", "outcome": "success", "ts": time.time()},
    ]
    with open(rem_dir / "2026-04-04.jsonl", "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    main(["revert", "--remediation-id", "rem-001"])
    captured = capsys.readouterr()
    assert "Reverted" in captured.out

    # Verify file was updated
    from opentriage.io.reader import read_jsonl
    updated = read_jsonl(rem_dir / "2026-04-04.jsonl")
    assert updated[0]["outcome"] == "reverted"
    assert "reverted_at" in updated[0]
    assert updated[1]["outcome"] == "success"  # Other record unchanged


def test_revert_not_found(tmp_path, monkeypatch, capsys):
    _init(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main(["revert", "--remediation-id", "nonexistent"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "not found" in captured.err.lower()


# --- cleanup ---

def test_cleanup_dry_run(tmp_path, monkeypatch, capsys):
    ot_dir, _ = _init(tmp_path, monkeypatch)

    # Write old correlation and metric files
    (ot_dir / "correlations" / "2025-01-01.jsonl").write_text('{"old": true}\n')
    (ot_dir / "metrics" / "2025-01-01.json").write_text('{"old": true}\n')
    # Write recent file
    (ot_dir / "correlations" / "2026-04-04.jsonl").write_text('{"recent": true}\n')

    main(["cleanup", "--older-than", "30", "--dry-run"])
    captured = capsys.readouterr()
    assert "Would remove" in captured.out
    assert "2025-01-01" in captured.out
    # Old files still exist
    assert (ot_dir / "correlations" / "2025-01-01.jsonl").exists()


def test_cleanup_delete(tmp_path, monkeypatch, capsys):
    ot_dir, _ = _init(tmp_path, monkeypatch)

    (ot_dir / "correlations" / "2025-01-01.jsonl").write_text('{"old": true}\n')
    (ot_dir / "correlations" / "2026-04-04.jsonl").write_text('{"recent": true}\n')

    main(["cleanup", "--older-than", "30"])
    captured = capsys.readouterr()
    assert "Removed" in captured.out

    # Old file deleted, recent kept
    assert not (ot_dir / "correlations" / "2025-01-01.jsonl").exists()
    assert (ot_dir / "correlations" / "2026-04-04.jsonl").exists()


def test_cleanup_nothing_to_clean(tmp_path, monkeypatch, capsys):
    _init(tmp_path, monkeypatch)
    main(["cleanup"])
    captured = capsys.readouterr()
    assert "Removed: 0 files" in captured.out
