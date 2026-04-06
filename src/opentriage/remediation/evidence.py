"""Evidence bundle assembler for remediation (F-AR03).

Collects all diagnostic context for a classified error into a single
structured bundle that a fix agent can consume.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from opentriage.io.reader import (
    load_correlations,
    load_fingerprints,
    load_session_events,
    read_json,
)

log = logging.getLogger(__name__)

# Max size for evidence bundle (50KB per spec)
MAX_BUNDLE_SIZE_BYTES = 50 * 1024
MAX_SESSION_EVENTS = 20
MAX_RECENT_CORRELATIONS = 10
MAX_ERROR_TEXT_LEN = 500


def _sanitize_text(text: str, max_len: int = MAX_ERROR_TEXT_LEN) -> str:
    """Sanitize untrusted text: truncate, strip control chars (G3 defense)."""
    if not isinstance(text, str):
        text = str(text)
    # Strip control characters except newline/tab
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return text[:max_len]


@dataclass
class EvidenceBundle:
    """Structured evidence bundle for fix agent consumption (T1 defense)."""

    attempt_id: str
    error_event: dict[str, Any]
    screenshot_path: str | None
    screenshot_note: str | None
    fingerprint: dict[str, Any]
    session_events: list[dict[str, Any]]
    recent_correlations: list[dict[str, Any]]
    git_context: str | None
    relevant_files: list[str]
    remedy: dict[str, Any] | None = None
    project_dir: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)


def _validate_screenshot(path: str | None) -> tuple[str | None, str | None]:
    """Validate screenshot path exists and is readable (T2 defense)."""
    if not path:
        return None, None
    if not os.path.exists(path):
        return None, "File missing at assembly time"
    if not os.access(path, os.R_OK):
        return None, "File inaccessible (permission denied) at assembly time"
    return path, None


def _get_git_context(project_dir: Path | None) -> str | None:
    """Get recent git log and diff stat. Returns None if git unavailable."""
    if project_dir is None:
        return None
    try:
        log_result = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True, text=True, timeout=10,
            cwd=str(project_dir),
        )
        diff_result = subprocess.run(
            ["git", "diff", "HEAD~1", "--stat"],
            capture_output=True, text=True, timeout=10,
            cwd=str(project_dir),
        )
        parts = []
        if log_result.returncode == 0 and log_result.stdout.strip():
            parts.append(f"Recent commits:\n{log_result.stdout.strip()}")
        if diff_result.returncode == 0 and diff_result.stdout.strip():
            parts.append(f"Last commit diff stat:\n{diff_result.stdout.strip()}")
        return "\n\n".join(parts) if parts else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def assemble_evidence(
    correlation: dict[str, Any],
    openlog_dir: Path,
    opentriage_dir: Path,
    attempt_id: str,
    project_dir: Path | None = None,
) -> EvidenceBundle:
    """Assemble all diagnostic context for a remediation attempt.

    Args:
        correlation: The triage correlation record that triggered remediation.
        openlog_dir: Path to .openlog/ directory.
        opentriage_dir: Path to .opentriage/ directory.
        attempt_id: Unique ID for this remediation attempt.
        project_dir: Path to the project being remediated (for git context).

    Returns:
        EvidenceBundle with all available diagnostic context.
    """
    # 1. Error event - sanitize untrusted fields (G3 defense)
    error_event = dict(correlation)
    for key in ("f_raw", "stderr"):
        if key in error_event and isinstance(error_event[key], str):
            error_event[key] = _sanitize_text(error_event[key])
    if "data" in error_event and isinstance(error_event["data"], dict):
        for k, v in error_event["data"].items():
            if isinstance(v, str):
                error_event["data"][k] = _sanitize_text(v)

    # 2. Screenshot path validation (T2 defense)
    screenshot_raw = None
    if isinstance(error_event.get("data"), dict):
        screenshot_raw = error_event["data"].get("screenshot")
    screenshot_path, screenshot_note = _validate_screenshot(screenshot_raw)

    # 3. Fingerprint with structured remedy
    slug = correlation.get("matched_fingerprint", "")
    fingerprints = load_fingerprints(openlog_dir)
    fp_map = {fp.get("slug", ""): fp for fp in fingerprints}
    fingerprint = fp_map.get(slug, {"slug": slug})
    remedy = fingerprint.get("remedy")

    # 4. Session events (last N)
    session_id = correlation.get("session_id", "")
    all_session_events = load_session_events(openlog_dir, session_id)
    session_events = all_session_events[-MAX_SESSION_EVENTS:]

    # 5. Recent correlations for the same fingerprint
    all_correlations = load_correlations(opentriage_dir)
    recent_corrs = [
        c for c in all_correlations
        if c.get("matched_fingerprint") == slug
    ][-MAX_RECENT_CORRELATIONS:]

    # 6. Git context
    git_context = _get_git_context(project_dir)

    # 7. Relevant files from structured remedy or fingerprint
    relevant_files: list[str] = []
    if isinstance(remedy, dict):
        relevant_files = list(remedy.get("relevant_files", []))
    if not relevant_files and fingerprint.get("ref"):
        # Infer from ref field if available
        ref = fingerprint["ref"]
        if isinstance(ref, str) and ("." in ref or "/" in ref):
            relevant_files = [ref]

    bundle = EvidenceBundle(
        attempt_id=attempt_id,
        error_event=error_event,
        screenshot_path=screenshot_path,
        screenshot_note=screenshot_note,
        fingerprint=fingerprint,
        session_events=session_events,
        recent_correlations=recent_corrs,
        git_context=git_context,
        relevant_files=relevant_files,
        remedy=remedy,
        project_dir=str(project_dir) if project_dir else None,
    )

    # Enforce 50KB limit — truncate session events if needed
    bundle_json = bundle.to_json()
    while len(bundle_json.encode()) > MAX_BUNDLE_SIZE_BYTES and bundle.session_events:
        bundle.session_events = bundle.session_events[1:]
        bundle_json = bundle.to_json()

    return bundle


def write_evidence_bundle(
    opentriage_dir: Path,
    bundle: EvidenceBundle,
) -> Path:
    """Write evidence bundle to disk. Returns the path written."""
    rem_dir = opentriage_dir / "remediations" / bundle.attempt_id
    rem_dir.mkdir(parents=True, exist_ok=True)
    path = rem_dir / "evidence.json"
    path.write_text(bundle.to_json())
    return path
