"""Tests for evidence bundle assembler (F-AR03)."""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

from opentriage.remediation.evidence import (
    EvidenceBundle,
    _sanitize_text,
    _validate_screenshot,
    assemble_evidence,
    write_evidence_bundle,
)
from tests.conftest import write_events, write_fingerprints


def test_sanitize_text_strips_control_chars():
    raw = "hello\x00world\x07test\x1b"
    result = _sanitize_text(raw)
    assert result == "helloworldtest"


def test_sanitize_text_truncates():
    long = "a" * 1000
    result = _sanitize_text(long, max_len=100)
    assert len(result) == 100


def test_sanitize_text_handles_non_string():
    assert _sanitize_text(12345) == "12345"


def test_validate_screenshot_none():
    path, note = _validate_screenshot(None)
    assert path is None
    assert note is None


def test_validate_screenshot_missing(tmp_path):
    path, note = _validate_screenshot(str(tmp_path / "nonexistent.png"))
    assert path is None
    assert "missing" in note.lower()


def test_validate_screenshot_exists(tmp_path):
    img = tmp_path / "screen.png"
    img.write_bytes(b"fake png")
    path, note = _validate_screenshot(str(img))
    assert path == str(img)
    assert note is None


def test_evidence_bundle_dataclass():
    bundle = EvidenceBundle(
        attempt_id="test-001",
        error_event={"f_raw": "error", "ref": "t1"},
        screenshot_path=None,
        screenshot_note=None,
        fingerprint={"slug": "test-slug"},
        session_events=[],
        recent_correlations=[],
        git_context=None,
        relevant_files=["src/foo.py"],
    )
    d = bundle.to_dict()
    assert d["attempt_id"] == "test-001"
    assert d["relevant_files"] == ["src/foo.py"]

    j = bundle.to_json()
    parsed = json.loads(j)
    assert parsed["attempt_id"] == "test-001"


def test_assemble_evidence_basic(tmp_dirs, sample_fingerprints, sample_events):
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, sample_fingerprints)
    write_events(ol_dir, sample_events, session="sess-001")

    correlation = {
        "ts": time.time(),
        "ref": "task-1",
        "session_id": "sess-001",
        "f_raw": "circular import between auth and user",
        "stderr": "ImportError: circular import",
        "matched_fingerprint": "circular-import",
        "classification": "known-pattern",
        "confidence": "high",
    }

    bundle = assemble_evidence(
        correlation=correlation,
        openlog_dir=ol_dir,
        opentriage_dir=ot_dir,
        attempt_id="rem-test-001",
    )

    assert isinstance(bundle, EvidenceBundle)
    assert bundle.attempt_id == "rem-test-001"
    assert bundle.error_event["f_raw"] == "circular import between auth and user"
    assert bundle.fingerprint["slug"] == "circular-import"
    assert bundle.screenshot_path is None
    assert len(bundle.session_events) <= 20


def test_assemble_evidence_sanitizes_fraw(tmp_dirs, sample_fingerprints):
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, sample_fingerprints)

    correlation = {
        "ts": time.time(),
        "ref": "t1",
        "session_id": "s1",
        "f_raw": "x" * 1000 + "\x00evil",
        "matched_fingerprint": "circular-import",
    }

    bundle = assemble_evidence(
        correlation=correlation,
        openlog_dir=ol_dir,
        opentriage_dir=ot_dir,
        attempt_id="rem-sanitize",
    )

    # f_raw should be truncated to 500 and control chars stripped
    assert len(bundle.error_event["f_raw"]) <= 500
    assert "\x00" not in bundle.error_event["f_raw"]


def test_assemble_evidence_with_screenshot(tmp_dirs, sample_fingerprints):
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, sample_fingerprints)

    # Create a fake screenshot
    screenshot = ol_dir / "screenshots" / "screen.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    screenshot.write_bytes(b"fake png data")

    correlation = {
        "ts": time.time(),
        "ref": "t1",
        "session_id": "s1",
        "f_raw": "circular import",
        "data": {"screenshot": str(screenshot)},
        "matched_fingerprint": "circular-import",
    }

    bundle = assemble_evidence(
        correlation=correlation,
        openlog_dir=ol_dir,
        opentriage_dir=ot_dir,
        attempt_id="rem-screenshot",
    )

    assert bundle.screenshot_path == str(screenshot)
    assert bundle.screenshot_note is None


def test_assemble_evidence_missing_screenshot(tmp_dirs, sample_fingerprints):
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, sample_fingerprints)

    correlation = {
        "ts": time.time(),
        "ref": "t1",
        "session_id": "s1",
        "f_raw": "circular import",
        "data": {"screenshot": "/nonexistent/path.png"},
        "matched_fingerprint": "circular-import",
    }

    bundle = assemble_evidence(
        correlation=correlation,
        openlog_dir=ol_dir,
        opentriage_dir=ot_dir,
        attempt_id="rem-noscreen",
    )

    assert bundle.screenshot_path is None
    assert bundle.screenshot_note is not None


def test_assemble_evidence_50kb_limit(tmp_dirs, sample_fingerprints):
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, sample_fingerprints)

    # Create many large events to exceed 50KB
    big_events = []
    for i in range(200):
        big_events.append({
            "ts": time.time() - i,
            "kind": "error",
            "ref": f"t{i}",
            "session_id": "sess-big",
            "f_raw": "x" * 400,
            "stderr": "y" * 400,
        })
    write_events(ol_dir, big_events, session="sess-big")

    correlation = {
        "ts": time.time(),
        "ref": "t0",
        "session_id": "sess-big",
        "f_raw": "circular import",
        "matched_fingerprint": "circular-import",
    }

    bundle = assemble_evidence(
        correlation=correlation,
        openlog_dir=ol_dir,
        opentriage_dir=ot_dir,
        attempt_id="rem-big",
    )

    # Bundle should be under 50KB
    bundle_size = len(bundle.to_json().encode())
    assert bundle_size <= 50 * 1024


def test_write_evidence_bundle(tmp_dirs):
    ot_dir, _ = tmp_dirs
    bundle = EvidenceBundle(
        attempt_id="rem-write-test",
        error_event={"f_raw": "test error"},
        screenshot_path=None,
        screenshot_note=None,
        fingerprint={"slug": "test"},
        session_events=[],
        recent_correlations=[],
        git_context=None,
        relevant_files=[],
    )

    path = write_evidence_bundle(ot_dir, bundle)
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["attempt_id"] == "rem-write-test"


def test_assemble_evidence_structured_remedy(tmp_dirs):
    ot_dir, ol_dir = tmp_dirs
    fps = [{
        "slug": "selector-drift",
        "patterns": ["selector not found"],
        "status": "confirmed",
        "severity": "recoverable",
        "remedy": {
            "strategy": "code-fix",
            "description": "Update selector",
            "relevant_files": ["src/selectors.ts"],
            "test_command": "npm test",
            "fix_prompt": "Fix the selector",
            "max_cost_usd": 2.0,
            "requires_screenshot": True,
        },
    }]
    write_fingerprints(ol_dir, fps)

    correlation = {
        "ts": time.time(),
        "ref": "t1",
        "session_id": "s1",
        "f_raw": "selector not found",
        "matched_fingerprint": "selector-drift",
    }

    bundle = assemble_evidence(
        correlation=correlation,
        openlog_dir=ol_dir,
        opentriage_dir=ot_dir,
        attempt_id="rem-structured",
    )

    assert bundle.relevant_files == ["src/selectors.ts"]
    assert bundle.remedy["strategy"] == "code-fix"
    assert bundle.remedy["fix_prompt"] == "Fix the selector"
