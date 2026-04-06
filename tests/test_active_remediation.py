"""Tests for active remediation features (F-AR02 through F-AR06)."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from opentriage.config import Config
from opentriage.io.reader import load_fingerprints, normalize_remedy
from opentriage.remediation.agent_handler import (
    RemediationResult,
    build_fix_prompt,
    verify_fix,
)
from opentriage.remediation.engine import (
    _check_circuit_breaker,
    _matches_skip_patterns,
    _update_circuit_breaker,
    record_pending_verification,
    run_remediation,
)
from opentriage.remediation.evidence import (
    EvidenceBundle,
    _sanitize_text,
    _validate_screenshot,
    assemble_evidence,
    write_evidence_bundle,
)
from opentriage.remediation.verification import (
    _count_active_minutes,
    add_pending_verification,
    check_recurrence,
    get_verification_summary,
)
from tests.conftest import write_events, write_fingerprints, write_state


# ── F-AR02: Structured Remedy Format ──


class TestNormalizeRemedy:
    def test_none_returns_none(self):
        assert normalize_remedy(None) is None

    def test_empty_string_returns_none(self):
        assert normalize_remedy("") is None
        assert normalize_remedy("   ") is None

    def test_string_becomes_escalate(self):
        result = normalize_remedy("Fix the import cycle")
        assert result == {"strategy": "escalate", "description": "Fix the import cycle"}

    def test_dict_preserved_with_defaults(self):
        result = normalize_remedy({"strategy": "code-fix", "description": "Fix it"})
        assert result["strategy"] == "code-fix"
        assert result["description"] == "Fix it"
        assert result["relevant_files"] == []
        assert result["max_cost_usd"] == 2.0
        assert result["requires_screenshot"] is False

    def test_dict_existing_fields_not_overwritten(self):
        result = normalize_remedy({
            "strategy": "code-fix",
            "description": "Fix selector",
            "relevant_files": ["src/bot.ts"],
            "max_cost_usd": 5.0,
        })
        assert result["relevant_files"] == ["src/bot.ts"]
        assert result["max_cost_usd"] == 5.0

    def test_non_string_non_dict_returns_none(self):
        assert normalize_remedy(42) is None
        assert normalize_remedy([1, 2]) is None


class TestLoadFingerprintsStructuredRemedy:
    def test_string_remedy_normalized(self, tmp_dirs):
        _, ol_dir = tmp_dirs
        fps = [{"slug": "test", "patterns": ["err"], "remedy": "Fix the bug"}]
        write_fingerprints(ol_dir, fps)
        loaded = load_fingerprints(ol_dir)
        assert len(loaded) == 1
        remedy = loaded[0]["remedy"]
        assert isinstance(remedy, dict)
        assert remedy["strategy"] == "escalate"
        assert remedy["description"] == "Fix the bug"

    def test_dict_remedy_preserved(self, tmp_dirs):
        _, ol_dir = tmp_dirs
        fps = [{
            "slug": "test",
            "patterns": ["err"],
            "remedy": {"strategy": "code-fix", "description": "Fix it", "relevant_files": ["a.py"]},
        }]
        write_fingerprints(ol_dir, fps)
        loaded = load_fingerprints(ol_dir)
        remedy = loaded[0]["remedy"]
        assert remedy["strategy"] == "code-fix"
        assert remedy["relevant_files"] == ["a.py"]

    def test_none_remedy_stays_none(self, tmp_dirs):
        _, ol_dir = tmp_dirs
        fps = [{"slug": "test", "patterns": ["err"], "remedy": None}]
        write_fingerprints(ol_dir, fps)
        loaded = load_fingerprints(ol_dir)
        assert loaded[0]["remedy"] is None

    def test_dict_format_fingerprints(self, tmp_dirs):
        _, ol_dir = tmp_dirs
        data = {
            "fingerprints": {
                "slug-a": {"patterns": ["err a"], "remedy": "Fix A"},
                "slug-b": {"patterns": ["err b"], "remedy": {"strategy": "code-fix", "description": "Fix B"}},
            }
        }
        (ol_dir / "fingerprints.json").write_text(json.dumps(data))
        loaded = load_fingerprints(ol_dir)
        assert len(loaded) == 2
        by_slug = {fp["slug"]: fp for fp in loaded}
        assert by_slug["slug-a"]["remedy"]["strategy"] == "escalate"
        assert by_slug["slug-b"]["remedy"]["strategy"] == "code-fix"


# ── F-AR03: Evidence Bundle Assembler ──


class TestSanitizeText:
    def test_strips_control_chars(self):
        assert _sanitize_text("hello\x00world\x07!") == "helloworld!"

    def test_preserves_newlines_tabs(self):
        assert _sanitize_text("line1\nline2\ttab") == "line1\nline2\ttab"

    def test_truncates(self):
        result = _sanitize_text("a" * 1000, max_len=100)
        assert len(result) == 100


class TestValidateScreenshot:
    def test_none_path(self):
        path, note = _validate_screenshot(None)
        assert path is None and note is None

    def test_missing_file(self):
        path, note = _validate_screenshot("/nonexistent/screenshot.png")
        assert path is None
        assert "missing" in note.lower()

    def test_existing_file(self, tmp_path):
        f = tmp_path / "screenshot.png"
        f.write_text("fake png")
        path, note = _validate_screenshot(str(f))
        assert path == str(f)
        assert note is None


class TestAssembleEvidence:
    def test_basic_assembly(self, tmp_dirs):
        ot_dir, ol_dir = tmp_dirs
        fps = [{"slug": "test-slug", "patterns": ["test error"], "remedy": {"strategy": "code-fix", "description": "Fix"}}]
        write_fingerprints(ol_dir, fps)

        corr = {
            "ts": time.time(),
            "ref": "task-1",
            "session_id": "sess-001",
            "f_raw": "test error occurred",
            "matched_fingerprint": "test-slug",
        }
        bundle = assemble_evidence(corr, ol_dir, ot_dir, "test-attempt-001")
        assert bundle.attempt_id == "test-attempt-001"
        assert bundle.fingerprint.get("slug") == "test-slug"
        assert bundle.remedy is not None
        assert bundle.remedy["strategy"] == "code-fix"

    def test_sanitizes_error_text(self, tmp_dirs):
        ot_dir, ol_dir = tmp_dirs
        fps = [{"slug": "s", "patterns": ["x"], "remedy": "fix"}]
        write_fingerprints(ol_dir, fps)

        corr = {
            "ts": time.time(),
            "ref": "t1",
            "session_id": "s1",
            "f_raw": "error\x00with\x07control",
            "matched_fingerprint": "s",
        }
        bundle = assemble_evidence(corr, ol_dir, ot_dir, "att-001")
        assert "\x00" not in bundle.error_event["f_raw"]
        assert "\x07" not in bundle.error_event["f_raw"]

    def test_serializable(self, tmp_dirs):
        ot_dir, ol_dir = tmp_dirs
        fps = [{"slug": "s", "patterns": ["x"], "remedy": "fix"}]
        write_fingerprints(ol_dir, fps)
        corr = {"ts": time.time(), "ref": "t1", "session_id": "s1", "f_raw": "err", "matched_fingerprint": "s"}
        bundle = assemble_evidence(corr, ol_dir, ot_dir, "att-002")
        json_str = bundle.to_json()
        parsed = json.loads(json_str)
        assert parsed["attempt_id"] == "att-002"


class TestWriteEvidenceBundle:
    def test_writes_to_disk(self, tmp_dirs):
        ot_dir, ol_dir = tmp_dirs
        fps = [{"slug": "s", "patterns": ["x"], "remedy": "fix"}]
        write_fingerprints(ol_dir, fps)
        corr = {"ts": time.time(), "ref": "t1", "session_id": "s1", "f_raw": "err", "matched_fingerprint": "s"}
        bundle = assemble_evidence(corr, ol_dir, ot_dir, "att-003")
        path = write_evidence_bundle(ot_dir, bundle)
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["attempt_id"] == "att-003"


# ── F-AR04: Fix Agent Spawner ──


class TestBuildFixPrompt:
    def test_contains_error_info(self):
        bundle = EvidenceBundle(
            attempt_id="test",
            error_event={"f_raw": "selector not found", "stderr": "Error"},
            screenshot_path=None,
            screenshot_note=None,
            fingerprint={"slug": "selector-drift"},
            session_events=[],
            recent_correlations=[],
            git_context=None,
            relevant_files=["src/bot.ts"],
            remedy={"strategy": "code-fix", "description": "Update selector", "fix_prompt": "Fix the button selector"},
        )
        prompt = build_fix_prompt(bundle)
        assert "selector not found" in prompt
        assert "selector-drift" in prompt
        assert "src/bot.ts" in prompt
        assert "Update selector" in prompt
        assert "Fix the button selector" in prompt
        assert "Do NOT modify test expectations" in prompt
        assert "One bug, one fix" in prompt

    def test_includes_screenshot_instruction(self):
        bundle = EvidenceBundle(
            attempt_id="test",
            error_event={"f_raw": "err"},
            screenshot_path="/tmp/screenshot.png",
            screenshot_note=None,
            fingerprint={"slug": "s"},
            session_events=[],
            recent_correlations=[],
            git_context=None,
            relevant_files=[],
            remedy={"strategy": "code-fix", "description": "fix"},
        )
        prompt = build_fix_prompt(bundle)
        assert "/tmp/screenshot.png" in prompt
        assert "Screenshot" in prompt


class TestVerifyFix:
    def test_empty_diff_fails(self, tmp_path):
        # Init a git repo with no changes
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=str(tmp_path), capture_output=True)
        bundle = EvidenceBundle(
            attempt_id="test", error_event={}, screenshot_path=None,
            screenshot_note=None, fingerprint={}, session_events=[],
            recent_correlations=[], git_context=None, relevant_files=["a.py"],
        )
        failures = verify_fix(bundle, str(tmp_path))
        assert any("No files changed" in f for f in failures)


# ── F-AR05: Orchestrator ──


class TestSkipPatterns:
    def test_matches_antml_thinking(self):
        cfg = Config()
        assert _matches_skip_patterns("antml:thinking something", cfg) is True

    def test_matches_antml_artifact(self):
        cfg = Config()
        assert _matches_skip_patterns("antml:some_artifact error", cfg) is True

    def test_no_match_normal_error(self):
        cfg = Config()
        assert _matches_skip_patterns("circular import error", cfg) is False

    def test_custom_skip_patterns(self):
        cfg = Config()
        cfg.remediation["skip_patterns"] = [r"ignore_this"]
        assert _matches_skip_patterns("ignore_this error", cfg) is True
        assert _matches_skip_patterns("normal error", cfg) is False


class TestCircuitBreaker:
    def test_no_breaker_allows(self, tmp_dirs):
        ot_dir, _ = tmp_dirs
        write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0"})
        ok, reason = _check_circuit_breaker("some-slug", ot_dir)
        assert ok is True

    def test_suspended_blocks(self, tmp_dirs):
        ot_dir, _ = tmp_dirs
        state = {
            "circuit_breaker": "full-autonomy",
            "version": "1.0",
            "circuit_breakers": {
                "bad-slug": {
                    "consecutive_failures": 3,
                    "suspended_until": time.time() + 3600,
                    "last_attempt_ts": time.time(),
                }
            }
        }
        write_state(ot_dir, state)
        ok, reason = _check_circuit_breaker("bad-slug", ot_dir)
        assert ok is False
        assert "circuit_breaker_suspended" in reason

    def test_update_on_success_resets(self, tmp_dirs):
        ot_dir, _ = tmp_dirs
        state = {
            "circuit_breaker": "full-autonomy",
            "version": "1.0",
            "circuit_breakers": {
                "slug": {"consecutive_failures": 2, "suspended_until": None, "last_attempt_ts": None}
            }
        }
        write_state(ot_dir, state)
        _update_circuit_breaker("slug", ot_dir, success=True)
        from opentriage.io.reader import read_json
        updated = read_json(ot_dir / "state.json")
        assert updated["circuit_breakers"]["slug"]["consecutive_failures"] == 0

    def test_update_on_failure_increments(self, tmp_dirs):
        ot_dir, _ = tmp_dirs
        write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0"})
        _update_circuit_breaker("slug", ot_dir, success=False)
        _update_circuit_breaker("slug", ot_dir, success=False)
        _update_circuit_breaker("slug", ot_dir, success=False)
        from opentriage.io.reader import read_json
        updated = read_json(ot_dir / "state.json")
        assert updated["circuit_breakers"]["slug"]["consecutive_failures"] == 3
        assert updated["circuit_breakers"]["slug"]["suspended_until"] is not None


class TestRunRemediationRouting:
    def test_fatal_severity_escalated(self, tmp_dirs):
        ot_dir, ol_dir = tmp_dirs
        fps = [{"slug": "fatal-bug", "patterns": ["fatal"], "status": "confirmed",
                "severity": "fatal", "remedy": "Review manually"}]
        write_fingerprints(ol_dir, fps)
        write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0"})

        cfg = Config()
        cfg.remediation["handler"] = "noop"
        corrs = [{"ts": time.time(), "ref": "t1", "session_id": "s1",
                  "classification": "known-pattern", "matched_fingerprint": "fatal-bug",
                  "confidence": "high"}]
        rems = run_remediation(corrs, cfg, ot_dir, ol_dir)
        assert len(rems) == 1
        assert rems[0]["outcome"] == "escalated_fatal"

    def test_skip_pattern_skipped(self, tmp_dirs):
        ot_dir, ol_dir = tmp_dirs
        fps = [{"slug": "thinking", "patterns": ["antml:thinking"], "status": "confirmed",
                "severity": "recoverable", "remedy": "ignore"}]
        write_fingerprints(ol_dir, fps)
        write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0"})

        cfg = Config()
        corrs = [{"ts": time.time(), "ref": "t1", "session_id": "s1",
                  "f_raw": "antml:thinking error here",
                  "classification": "known-pattern", "matched_fingerprint": "thinking",
                  "confidence": "high"}]
        rems = run_remediation(corrs, cfg, ot_dir, ol_dir)
        assert len(rems) == 1
        assert rems[0]["outcome"] == "skipped"

    def test_circuit_breaker_blocks(self, tmp_dirs):
        ot_dir, ol_dir = tmp_dirs
        fps = [{"slug": "broken", "patterns": ["broken"], "status": "confirmed",
                "severity": "recoverable", "remedy": "try fix"}]
        write_fingerprints(ol_dir, fps)
        state = {
            "circuit_breaker": "full-autonomy", "version": "1.0",
            "circuit_breakers": {
                "broken": {"consecutive_failures": 3, "suspended_until": time.time() + 3600,
                           "last_attempt_ts": time.time()}
            }
        }
        write_state(ot_dir, state)

        cfg = Config()
        corrs = [{"ts": time.time(), "ref": "t1", "session_id": "s1",
                  "classification": "known-pattern", "matched_fingerprint": "broken",
                  "confidence": "high"}]
        rems = run_remediation(corrs, cfg, ot_dir, ol_dir)
        assert len(rems) == 1
        assert rems[0]["outcome"] == "circuit_breaker_suspended"

    def test_restart_strategy(self, tmp_dirs):
        ot_dir, ol_dir = tmp_dirs
        fps = [{"slug": "restart-me", "patterns": ["crash"], "status": "confirmed",
                "severity": "recoverable",
                "remedy": {"strategy": "restart", "description": "Restart the bot"}}]
        write_fingerprints(ol_dir, fps)
        write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0"})

        cfg = Config()
        cfg.remediation["handler"] = "noop"
        corrs = [{"ts": time.time(), "ref": "t1", "session_id": "s1",
                  "classification": "known-pattern", "matched_fingerprint": "restart-me",
                  "confidence": "high"}]
        rems = run_remediation(corrs, cfg, ot_dir, ol_dir, project_dir=ot_dir.parent)
        assert len(rems) == 1
        assert rems[0]["outcome"] == "restart_requested"
        sentinel = ot_dir.parent / ".opentriage" / "restart_requested"
        assert sentinel.exists()

    def test_dedup_same_fingerprint_in_cycle(self, tmp_dirs):
        ot_dir, ol_dir = tmp_dirs
        fps = [{"slug": "dup", "patterns": ["dup error"], "status": "confirmed",
                "severity": "recoverable", "remedy": "Fix it"}]
        write_fingerprints(ol_dir, fps)
        write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0"})

        cfg = Config()
        cfg.remediation["handler"] = "noop"
        corrs = [
            {"ts": time.time(), "ref": "t1", "session_id": "s1",
             "classification": "known-pattern", "matched_fingerprint": "dup", "confidence": "high"},
            {"ts": time.time(), "ref": "t2", "session_id": "s2",
             "classification": "known-pattern", "matched_fingerprint": "dup", "confidence": "high"},
        ]
        rems = run_remediation(corrs, cfg, ot_dir, ol_dir)
        assert len(rems) == 1  # Only first remediated

    def test_structured_remedy_noop_handler(self, tmp_dirs):
        """Structured code-fix remedy with noop handler falls through to noop."""
        ot_dir, ol_dir = tmp_dirs
        fps = [{"slug": "code-bug", "patterns": ["code err"], "status": "confirmed",
                "severity": "recoverable",
                "remedy": {"strategy": "code-fix", "description": "Fix the code",
                           "relevant_files": ["src/main.py"]}}]
        write_fingerprints(ol_dir, fps)
        write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0"})

        cfg = Config()
        cfg.remediation["handler"] = "noop"
        corrs = [{"ts": time.time(), "ref": "t1", "session_id": "s1",
                  "classification": "known-pattern", "matched_fingerprint": "code-bug",
                  "confidence": "high"}]
        rems = run_remediation(corrs, cfg, ot_dir, ol_dir)
        assert len(rems) == 1
        assert rems[0]["action"] == "noop"


# ── F-AR06: Recurrence Verification ──


class TestActiveMinutes:
    def test_no_events(self):
        assert _count_active_minutes([], 0) == 0

    def test_events_in_one_bucket(self):
        base = 1000.0
        events = [{"ts": base + 10}, {"ts": base + 20}, {"ts": base + 30}]
        assert _count_active_minutes(events, base) == 5

    def test_events_in_multiple_buckets(self):
        base = 1000.0
        events = [
            {"ts": base + 10},     # bucket 0
            {"ts": base + 310},    # bucket 1
            {"ts": base + 610},    # bucket 2
        ]
        assert _count_active_minutes(events, base) == 15  # 3 buckets * 5 min


class TestPendingVerification:
    def test_add_verification(self, tmp_dirs):
        ot_dir, _ = tmp_dirs
        write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0"})
        add_pending_verification(ot_dir, "slug-a", "att-001", commit_sha="abc123")
        summary = get_verification_summary(ot_dir)
        assert summary["pending_count"] == 1
        assert summary["pending"][0]["fingerprint_slug"] == "slug-a"
        assert summary["pending"][0]["commit_sha"] == "abc123"

    def test_no_duplicates(self, tmp_dirs):
        ot_dir, _ = tmp_dirs
        write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0"})
        add_pending_verification(ot_dir, "slug-a", "att-001")
        add_pending_verification(ot_dir, "slug-a", "att-001")
        summary = get_verification_summary(ot_dir)
        assert summary["pending_count"] == 1


class TestCheckRecurrence:
    def test_no_pending_returns_empty(self, tmp_dirs):
        ot_dir, ol_dir = tmp_dirs
        write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0"})
        results = check_recurrence(ot_dir, ol_dir)
        assert results == []

    def test_recurrence_detected(self, tmp_dirs):
        ot_dir, ol_dir = tmp_dirs
        now = time.time()
        state = {
            "circuit_breaker": "full-autonomy", "version": "1.0",
            "pending_verifications": [{
                "fingerprint_slug": "test-slug",
                "fixed_at_ts": now - 3600,
                "attempt_id": "att-001",
                "commit_sha": "abc",
                "recurrence_window_hours": 6,
                "active_minutes_post_fix": 0,
                "status": "pending",
            }]
        }
        write_state(ot_dir, state)

        # Write a correlation that matches the fingerprint after fix
        from opentriage.io.writer import write_correlation
        write_correlation(ot_dir, {
            "ts": now - 1800,
            "ref": "t2",
            "session_id": "s2",
            "matched_fingerprint": "test-slug",
            "classification": "known-pattern",
            "confidence": "high",
        })

        results = check_recurrence(ot_dir, ol_dir)
        assert len(results) == 1
        assert results[0]["status"] == "recurred"

    def test_verified_after_window(self, tmp_dirs):
        ot_dir, ol_dir = tmp_dirs
        now = time.time()
        state = {
            "circuit_breaker": "full-autonomy", "version": "1.0",
            "pending_verifications": [{
                "fingerprint_slug": "fixed-slug",
                "fixed_at_ts": now - 25200,  # 7 hours ago
                "attempt_id": "att-002",
                "commit_sha": "def",
                "recurrence_window_hours": 6,
                "active_minutes_post_fix": 0,
                "status": "pending",
            }]
        }
        write_state(ot_dir, state)

        # Write enough events to meet the active minutes threshold
        events = []
        for i in range(20):
            events.append({
                "ts": now - 25000 + (i * 400),
                "kind": "error",
                "ref": f"t-{i}",
                "session_id": "active-sess",
                "f_raw": "some other unrelated error",
            })
        write_events(ol_dir, events, "active-sess")

        results = check_recurrence(ot_dir, ol_dir)
        assert len(results) == 1
        assert results[0]["status"] == "verified"


class TestRecordPendingVerification:
    def test_only_records_for_fixed(self, tmp_dirs):
        ot_dir, _ = tmp_dirs
        write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0"})
        # Should not record for failed outcome
        record_pending_verification(ot_dir, {"outcome": "failed", "fingerprint_slug": "s", "ts": time.time()})
        summary = get_verification_summary(ot_dir)
        assert summary["pending_count"] == 0

    def test_records_for_fixed(self, tmp_dirs):
        ot_dir, _ = tmp_dirs
        write_state(ot_dir, {"circuit_breaker": "full-autonomy", "version": "1.0"})
        record_pending_verification(ot_dir, {
            "outcome": "fixed",
            "fingerprint_slug": "s",
            "attempt_id": "att-x",
            "commit_sha": "sha123",
            "ts": time.time(),
        })
        summary = get_verification_summary(ot_dir)
        assert summary["pending_count"] == 1
