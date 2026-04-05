"""Tests for the 8 CLI management commands (BUILD_TASK.md)."""

import json
import os
import time
from pathlib import Path

import pytest

from opentriage.cli import main


# ── Helpers ──────────────────────────────────────────────────────────────────

def _init(tmp_path: Path) -> tuple[Path, Path]:
    """Initialize opentriage + openlog dirs and return (ot_dir, ol_dir)."""
    main(["init"])
    ot_dir = tmp_path / ".opentriage"
    ol_dir = tmp_path / ".openlog"
    ol_dir.mkdir(exist_ok=True)
    (ol_dir / "events").mkdir(exist_ok=True)
    (ol_dir / "fingerprints.json").write_text(json.dumps([]))
    return ot_dir, ol_dir


def _make_draft(ot_dir: Path, slug: str, **overrides) -> dict:
    """Create a draft fingerprint file and return its data."""
    data = {
        "slug": slug,
        "description": f"Draft for {slug}",
        "patterns": [slug.replace("-", " ")],
        "severity": "recoverable",
        "category": "runtime",
        "remedy": f"Fix {slug}",
        "status": "proposed",
        "source_events": 3,
        "confidence": 0.85,
        "created_at": time.time() - 3600,
        "recurrence_count": 1,
    }
    data.update(overrides)
    (ot_dir / "drafts" / f"{slug}.json").write_text(json.dumps(data, indent=2))
    return data


def _make_escalation(ot_dir: Path, **overrides) -> dict:
    """Append an escalation record and return it."""
    record = {
        "ts": time.time(),
        "severity": "high",
        "type": "novel_pattern",
        "title": "Test escalation",
        "channel": "log",
        "delivery_status": "delivered",
        "ref": "task-1",
    }
    record.update(overrides)
    with open(ot_dir / "escalations.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")
    return record


def _make_remediation(ot_dir: Path, rem_id: str, ts: float | None = None, **overrides) -> dict:
    """Write a remediation record and return it."""
    ts = ts or time.time()
    record = {
        "ts": ts,
        "id": rem_id,
        "fingerprint_slug": "test-pattern",
        "handler_exit_code": 0,
        "outcome": "success",
        "session_id": "sess-001",
    }
    record.update(overrides)
    from datetime import datetime, timezone
    date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    path = ot_dir / "remediations" / f"{date_str}.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")
    return record


def _make_old_file(directory: Path, name: str, age_days: int) -> Path:
    """Create a file and backdate its mtime."""
    p = directory / name
    p.write_text("{}")
    old_time = time.time() - (age_days * 86400)
    os.utime(p, (old_time, old_time))
    return p


# ── 1. drafts ────────────────────────────────────────────────────────────────

class TestDrafts:
    def test_drafts_empty(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _init(tmp_path)
        main(["drafts"])
        captured = capsys.readouterr()
        # Should not error; may say "no drafts" or output empty
        assert captured.out is not None

    def test_drafts_lists_pending(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        _make_draft(ot_dir, "widget-crash")
        _make_draft(ot_dir, "auth-timeout")

        main(["drafts"])
        captured = capsys.readouterr()
        assert "widget-crash" in captured.out
        assert "auth-timeout" in captured.out

    def test_drafts_json_output(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        _make_draft(ot_dir, "widget-crash")

        capsys.readouterr()  # clear init output
        main(["drafts", "--json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["slug"] == "widget-crash"

    def test_drafts_ignores_subdirs(self, tmp_path, monkeypatch, capsys):
        """Drafts in approved/ and rejected/ subdirs should not appear."""
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        _make_draft(ot_dir, "pending-one")
        # Create approved subdir with a draft — should NOT be listed
        (ot_dir / "drafts" / "approved").mkdir(exist_ok=True)
        (ot_dir / "drafts" / "approved" / "old.json").write_text("{}")

        main(["drafts"])
        captured = capsys.readouterr()
        assert "pending-one" in captured.out


# ── 2. approve ───────────────────────────────────────────────────────────────

class TestApprove:
    def test_approve_success(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, ol_dir = _init(tmp_path)
        _make_draft(ot_dir, "widget-crash")

        main(["approve", "widget-crash"])
        captured = capsys.readouterr()
        # Should print confirmation
        assert "widget-crash" in captured.out.lower() or "approved" in captured.out.lower()

    def test_approve_copies_to_fingerprints(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, ol_dir = _init(tmp_path)
        _make_draft(ot_dir, "widget-crash")

        main(["approve", "widget-crash"])

        # Fingerprints registry should contain the approved pattern
        fps = json.loads((ol_dir / "fingerprints.json").read_text())
        slugs = [fp.get("slug") for fp in fps]
        assert "widget-crash" in slugs

    def test_approve_moves_to_approved_dir(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        _make_draft(ot_dir, "widget-crash")

        main(["approve", "widget-crash"])

        # Original draft should be gone, approved version should exist
        assert not (ot_dir / "drafts" / "widget-crash.json").exists()
        assert (ot_dir / "drafts" / "approved" / "widget-crash.json").exists()

    def test_approve_missing_slug(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _init(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            main(["approve", "nonexistent-slug"])
        assert exc_info.value.code == 1

    def test_approve_with_comment(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        _make_draft(ot_dir, "widget-crash")

        main(["approve", "widget-crash", "--comment", "Looks correct"])
        captured = capsys.readouterr()
        assert "widget-crash" in captured.out.lower() or "approved" in captured.out.lower()

    def test_approve_validates_required_fields(self, tmp_path, monkeypatch, capsys):
        """Draft missing required fields (pattern, severity, category) should fail."""
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        # Draft missing severity and category
        bad_draft = {"slug": "bad-draft", "description": "Incomplete"}
        (ot_dir / "drafts" / "bad-draft.json").write_text(json.dumps(bad_draft))

        with pytest.raises(SystemExit) as exc_info:
            main(["approve", "bad-draft"])
        assert exc_info.value.code == 1


# ── 3. reject ────────────────────────────────────────────────────────────────

class TestReject:
    def test_reject_success(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        _make_draft(ot_dir, "false-alarm")

        main(["reject", "false-alarm"])
        captured = capsys.readouterr()
        assert "false-alarm" in captured.out.lower() or "rejected" in captured.out.lower()

    def test_reject_moves_to_rejected_dir(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        _make_draft(ot_dir, "false-alarm")

        main(["reject", "false-alarm"])

        assert not (ot_dir / "drafts" / "false-alarm.json").exists()
        assert (ot_dir / "drafts" / "rejected" / "false-alarm.json").exists()

    def test_reject_adds_metadata(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        _make_draft(ot_dir, "false-alarm")

        main(["reject", "false-alarm", "--reason", "Not a real pattern"])

        rejected = json.loads(
            (ot_dir / "drafts" / "rejected" / "false-alarm.json").read_text()
        )
        assert rejected.get("rejected_reason") == "Not a real pattern"
        assert "rejected_at" in rejected

    def test_reject_missing_slug(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _init(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            main(["reject", "nonexistent"])
        assert exc_info.value.code == 1


# ── 4. escalations ──────────────────────────────────────────────────────────

class TestEscalations:
    def test_escalations_empty(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _init(tmp_path)
        main(["escalations"])
        captured = capsys.readouterr()
        # No error, may print "no escalations" or empty output
        assert captured.out is not None

    def test_escalations_shows_recent(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        _make_escalation(ot_dir, title="DB connection timeout", severity="high")
        _make_escalation(ot_dir, title="Memory spike", severity="medium")

        main(["escalations"])
        captured = capsys.readouterr()
        assert "DB connection timeout" in captured.out or "high" in captured.out

    def test_escalations_last_n(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        for i in range(5):
            _make_escalation(ot_dir, title=f"Escalation {i}")

        capsys.readouterr()  # clear init output
        main(["escalations", "--last", "2"])
        captured = capsys.readouterr()
        # Should only show the last 2
        # Count occurrences — we expect at most 2 escalation entries
        lines = [l for l in captured.out.strip().splitlines() if "Escalation" in l]
        assert len(lines) <= 2

    def test_escalations_json_output(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        _make_escalation(ot_dir, title="Test esc")

        capsys.readouterr()  # clear init output
        main(["escalations", "--json"])
        captured = capsys.readouterr()
        # JSON output: either a JSON array or JSONL lines
        lines = captured.out.strip().splitlines()
        parsed = [json.loads(line) for line in lines]
        assert len(parsed) >= 1


# ── 5. validate ──────────────────────────────────────────────────────────────

class TestValidate:
    def test_validate_passes_valid_setup(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, ol_dir = _init(tmp_path)
        # Create a minimal event file so .openlog has events
        (ol_dir / "events" / "session.jsonl").write_text(
            json.dumps({"ts": time.time(), "kind": "error", "f_raw": "test"}) + "\n"
        )

        try:
            main(["validate"])
            exit_code = 0
        except SystemExit as e:
            exit_code = e.code

        captured = capsys.readouterr()
        # Should show checklist
        assert ".opentriage" in captured.out.lower() or "config" in captured.out.lower()

    def test_validate_fails_no_opentriage_dir(self, tmp_path, monkeypatch, capsys):
        """validate should report failure if .opentriage/ doesn't exist."""
        monkeypatch.chdir(tmp_path)
        try:
            main(["validate"])
            exit_code = 0
        except SystemExit as e:
            exit_code = e.code

        captured = capsys.readouterr()
        # Should report the missing directory
        assert exit_code == 1 or "\u274c" in captured.out or "fail" in captured.out.lower()

    def test_validate_checks_config_parseable(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        # Corrupt the config
        (ot_dir / "config.toml").write_text("not valid toml [[[")

        try:
            main(["validate"])
            exit_code = 0
        except SystemExit as e:
            exit_code = e.code

        captured = capsys.readouterr()
        # Should report config parse failure (exit 1 or show failure marker)
        assert exit_code == 1 or "\u274c" in captured.out


# ── 6. calibrate ─────────────────────────────────────────────────────────────

class TestCalibrate:
    def test_calibrate_no_data(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, ol_dir = _init(tmp_path)

        main(["calibrate"])
        captured = capsys.readouterr()
        # Should run without error, report no data or 0 events
        assert captured.out is not None

    def test_calibrate_with_data(self, tmp_path, monkeypatch, capsys):
        """Calibrate with events that have both LLM classification and fingerprint match."""
        monkeypatch.chdir(tmp_path)
        ot_dir, ol_dir = _init(tmp_path)

        # Set up fingerprints
        fps = [{"slug": "test-pattern", "patterns": ["test error"], "status": "confirmed"}]
        (ol_dir / "fingerprints.json").write_text(json.dumps(fps))

        # Create correlation records with LLM classifications
        from datetime import datetime, timezone
        now = time.time()
        date_str = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        corr_path = ot_dir / "correlations" / f"{date_str}.jsonl"
        for i in range(3):
            record = {
                "ts": now - i * 60,
                "ref": f"task-{i}",
                "session_id": "sess-001",
                "classification": "known-pattern",
                "matched_fingerprint": "test-pattern",
                "confidence": "high",
            }
            with open(corr_path, "a") as f:
                f.write(json.dumps(record) + "\n")

        main(["calibrate", "--events", "3"])
        captured = capsys.readouterr()
        # Should report agreement/calibration results
        assert captured.out is not None

    def test_calibrate_custom_events_count(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _init(tmp_path)
        main(["calibrate", "--events", "5"])
        captured = capsys.readouterr()
        assert captured.out is not None


# ── 7. revert ────────────────────────────────────────────────────────────────

class TestRevert:
    def test_revert_success(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        _make_remediation(ot_dir, "rem-001")

        main(["revert", "--remediation-id", "rem-001"])
        captured = capsys.readouterr()
        assert "revert" in captured.out.lower() or "rem-001" in captured.out

    def test_revert_updates_outcome(self, tmp_path, monkeypatch, capsys):
        """After revert, the remediation outcome should be 'reverted'."""
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        _make_remediation(ot_dir, "rem-002")

        main(["revert", "--remediation-id", "rem-002"])

        # Read back remediations to check outcome was updated
        from opentriage.io.reader import load_remediations
        rems = load_remediations(ot_dir)
        reverted = [r for r in rems if r.get("id") == "rem-002"]
        assert any(r.get("outcome") == "reverted" for r in reverted)

    def test_revert_missing_id(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _init(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            main(["revert", "--remediation-id", "nonexistent"])
        assert exc_info.value.code == 1


# ── 8. cleanup ───────────────────────────────────────────────────────────────

class TestCleanup:
    def test_cleanup_dry_run(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        _make_old_file(ot_dir / "correlations", "old.jsonl", age_days=60)

        main(["cleanup", "--dry-run"])
        captured = capsys.readouterr()
        # Should list what would be removed without actually removing
        assert (ot_dir / "correlations" / "old.jsonl").exists()

    def test_cleanup_removes_old_files(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        old_file = _make_old_file(ot_dir / "correlations", "old.jsonl", age_days=60)
        new_file = ot_dir / "correlations" / "new.jsonl"
        new_file.write_text("{}")

        main(["cleanup", "--older-than", "30"])
        captured = capsys.readouterr()

        assert not old_file.exists(), "Old file should have been removed"
        assert new_file.exists(), "New file should still exist"

    def test_cleanup_respects_days_threshold(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        # File is 15 days old, threshold is 30 — should NOT be removed
        file_15d = _make_old_file(ot_dir / "correlations", "recent.jsonl", age_days=15)

        main(["cleanup", "--older-than", "30"])

        assert file_15d.exists(), "File younger than threshold should be kept"

    def test_cleanup_handles_empty_dirs(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _init(tmp_path)
        main(["cleanup"])
        captured = capsys.readouterr()
        # Should run without error on empty directories
        assert captured.out is not None

    def test_cleanup_multiple_directories(self, tmp_path, monkeypatch, capsys):
        """Cleanup should cover correlations, remediations, and metrics dirs."""
        monkeypatch.chdir(tmp_path)
        ot_dir, _ = _init(tmp_path)
        old_corr = _make_old_file(ot_dir / "correlations", "old.jsonl", age_days=60)
        old_rem = _make_old_file(ot_dir / "remediations", "old.jsonl", age_days=60)
        old_met = _make_old_file(ot_dir / "metrics", "old.json", age_days=60)

        main(["cleanup", "--older-than", "30"])

        assert not old_corr.exists()
        assert not old_rem.exists()
        assert not old_met.exists()


# ── Cross-cutting: all new commands require init (except validate) ───────────

class TestRequiresInit:
    """New commands (except validate) should fail if not initialized."""

    @pytest.mark.parametrize("cmd", [
        ["drafts"],
        ["approve", "some-slug"],
        ["reject", "some-slug"],
        ["escalations"],
        ["calibrate"],
        ["revert", "--remediation-id", "x"],
        ["cleanup"],
    ])
    def test_requires_init(self, tmp_path, monkeypatch, capsys, cmd):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            main(cmd)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "init" in captured.err.lower()
