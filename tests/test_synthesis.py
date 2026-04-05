"""Tests for novel pattern synthesis (F-OT06)."""

import json
import time
from pathlib import Path

from opentriage.synthesis.drafter import run_synthesis
from tests.conftest import MockProvider, write_fingerprints


def test_synthesis_creates_draft(tmp_dirs, sample_fingerprints):
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, sample_fingerprints)

    draft_response = json.dumps({
        "slug": "widget-factory-error",
        "description": "Widget factory crashes during module initialization",
        "patterns": ["widget factory explosion", "widget factory error"],
        "severity": "fatal",
        "remedy": "Check widget configuration and module dependencies",
        "root_cause_hypothesis": "HYPOTHESIS — Widget factory has circular dependency on module loader",
        "dedup_check": "This is NOT a variant of circular-import because it involves the widget subsystem",
    })

    provider = MockProvider({"expensive": draft_response})

    correlations = [
        {
            "ts": time.time(),
            "ref": "t1",
            "session_id": "s1",
            "f_raw": "widget factory explosion in module X",
            "classification": "novel",
            "confidence": "high",
            "reasoning": "No matching pattern",
        },
    ]

    drafts = run_synthesis(correlations, provider, ot_dir, ol_dir)
    assert len(drafts) == 1
    assert drafts[0]["slug"] == "widget-factory-error"
    assert "HYPOTHESIS" in drafts[0]["root_cause_hypothesis"]
    assert drafts[0]["status"] == "proposed"

    # Draft file should exist
    draft_path = ot_dir / "drafts" / "widget-factory-error.json"
    assert draft_path.exists()


def test_synthesis_minimal_draft_no_provider(tmp_dirs, sample_fingerprints):
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, sample_fingerprints)

    correlations = [
        {
            "ts": time.time(),
            "ref": "t1",
            "session_id": "s1",
            "f_raw": "some novel error",
            "classification": "novel",
            "confidence": "high",
        },
    ]

    drafts = run_synthesis(correlations, None, ot_dir, ol_dir)
    assert len(drafts) == 1
    assert drafts[0]["status"] == "incomplete"


def test_synthesis_recurrence_count(tmp_dirs, sample_fingerprints):
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, sample_fingerprints)

    draft_response = json.dumps({
        "slug": "recurring-error",
        "description": "A recurring error",
        "patterns": ["recurring error"],
        "severity": None,
        "remedy": None,
        "root_cause_hypothesis": "HYPOTHESIS — unknown",
        "dedup_check": "Not a variant",
    })
    provider = MockProvider({"expensive": draft_response})

    corr = {
        "ts": time.time(), "ref": "t1", "session_id": "s1",
        "f_raw": "recurring error", "classification": "novel", "confidence": "high",
    }

    # First time
    drafts = run_synthesis([corr], provider, ot_dir, ol_dir)
    assert drafts[0]["recurrence_count"] == 1

    # Second time
    drafts = run_synthesis([corr], provider, ot_dir, ol_dir)
    assert drafts[0]["recurrence_count"] == 2

    # Third time
    drafts = run_synthesis([corr], provider, ot_dir, ol_dir)
    assert drafts[0]["recurrence_count"] == 3


def test_synthesis_skips_non_novel(tmp_dirs, sample_fingerprints):
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, sample_fingerprints)

    correlations = [
        {"ts": time.time(), "ref": "t1", "session_id": "s1",
         "classification": "known-pattern", "confidence": "high"},
    ]

    drafts = run_synthesis(correlations, None, ot_dir, ol_dir)
    assert len(drafts) == 0


def test_synthesis_slug_collision(tmp_dirs, sample_fingerprints):
    """Draft slug colliding with existing fingerprint gets -draft suffix."""
    ot_dir, ol_dir = tmp_dirs
    write_fingerprints(ol_dir, sample_fingerprints)

    draft_response = json.dumps({
        "slug": "circular-import",  # Collides with existing
        "description": "A different circular import issue",
        "patterns": ["some new pattern"],
        "severity": None,
        "remedy": None,
        "root_cause_hypothesis": "HYPOTHESIS — variant",
        "dedup_check": "Different from existing",
    })
    provider = MockProvider({"expensive": draft_response})

    correlations = [
        {"ts": time.time(), "ref": "t1", "session_id": "s1",
         "f_raw": "new error", "classification": "novel", "confidence": "high"},
    ]

    drafts = run_synthesis(correlations, provider, ot_dir, ol_dir)
    assert drafts[0]["slug"] == "circular-import-draft"
