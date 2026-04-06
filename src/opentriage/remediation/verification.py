"""Recurrence verification for active remediation (F-AR06).

After a fix is applied, verifies the error doesn't recur within the
recurrence window (6 hours default). Tracks active time post-fix.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from opentriage.io.reader import load_correlations, read_json, scan_events
from opentriage.io.writer import write_json

log = logging.getLogger(__name__)

DEFAULT_RECURRENCE_WINDOW_HOURS = 6
ACTIVE_MINUTE_EVENT_INTERVAL = 300  # 5 minutes = 1 active-minute bucket
MIN_ACTIVE_MINUTES_FOR_VERIFICATION = 60


def add_pending_verification(
    opentriage_dir: Path,
    fingerprint_slug: str,
    attempt_id: str,
    commit_sha: str | None = None,
    recurrence_window_hours: float = DEFAULT_RECURRENCE_WINDOW_HOURS,
) -> None:
    """Record a pending verification after a successful fix."""
    state_path = opentriage_dir / "state.json"
    state = read_json(state_path)
    pending = state.setdefault("pending_verifications", [])

    # Don't duplicate
    for pv in pending:
        if pv.get("attempt_id") == attempt_id:
            return

    pending.append({
        "fingerprint_slug": fingerprint_slug,
        "fixed_at_ts": time.time(),
        "attempt_id": attempt_id,
        "commit_sha": commit_sha,
        "recurrence_window_hours": recurrence_window_hours,
        "active_minutes_post_fix": 0,
        "status": "pending",
    })
    write_json(state_path, state)


def check_recurrence(
    opentriage_dir: Path,
    openlog_dir: Path,
) -> list[dict[str, Any]]:
    """Check pending verifications for recurrence or expiry.

    Returns list of verification results with status updates.
    """
    state_path = opentriage_dir / "state.json"
    state = read_json(state_path)
    pending = state.get("pending_verifications", [])
    if not pending:
        return []

    now = time.time()
    results: list[dict[str, Any]] = []
    all_correlations = load_correlations(opentriage_dir)
    all_events = scan_events(openlog_dir)
    updated_pending = []

    for pv in pending:
        if pv.get("status") != "pending":
            continue

        slug = pv.get("fingerprint_slug", "")
        fixed_at = pv.get("fixed_at_ts", 0)
        window_hours = pv.get("recurrence_window_hours", DEFAULT_RECURRENCE_WINDOW_HOURS)
        window_end = fixed_at + (window_hours * 3600)

        # Update active minutes post-fix (T5 defense)
        post_fix_events = [
            e for e in all_events
            if e.get("ts", 0) > fixed_at
        ]
        active_minutes = _count_active_minutes(post_fix_events, fixed_at)
        pv["active_minutes_post_fix"] = active_minutes

        # Check for recurrence: same fingerprint in correlations after fix
        post_fix_corrs = [
            c for c in all_correlations
            if c.get("matched_fingerprint") == slug
            and c.get("ts", 0) > fixed_at
        ]

        # Multiple errors of same type in one cycle count as single recurrence
        recurred = len(post_fix_corrs) > 0

        if recurred:
            pv["status"] = "recurred"
            pv["recurred_at_ts"] = post_fix_corrs[0].get("ts", now)
            results.append(dict(pv))
            log.warning("Fix %s recurred for fingerprint %s", pv.get("attempt_id"), slug)
        elif now > window_end and active_minutes >= MIN_ACTIVE_MINUTES_FOR_VERIFICATION:
            # Window expired with sufficient active time and no recurrence
            pv["status"] = "verified"
            pv["verified_at_ts"] = now
            results.append(dict(pv))
            log.info("Fix %s verified for fingerprint %s", pv.get("attempt_id"), slug)
        elif now > window_end and active_minutes < MIN_ACTIVE_MINUTES_FOR_VERIFICATION:
            # Window expired but not enough active time — extend (T5)
            pv["recurrence_window_hours"] = window_hours + DEFAULT_RECURRENCE_WINDOW_HOURS
            updated_pending.append(pv)
            log.info("Extended verification window for %s (only %d active minutes)",
                     pv.get("attempt_id"), active_minutes)
            continue
        else:
            # Still within window
            updated_pending.append(pv)
            continue

    # Update state
    state["pending_verifications"] = updated_pending
    write_json(state_path, state)

    return results


def _count_active_minutes(events: list[dict[str, Any]], since_ts: float) -> int:
    """Count active minutes by checking event timestamps (T5).

    "Active" = at least 1 event per 5-minute window.
    """
    if not events:
        return 0

    timestamps = sorted(e.get("ts", 0) for e in events if e.get("ts", 0) > since_ts)
    if not timestamps:
        return 0

    active_buckets: set[int] = set()
    for ts in timestamps:
        bucket = int((ts - since_ts) / ACTIVE_MINUTE_EVENT_INTERVAL)
        active_buckets.add(bucket)

    return len(active_buckets) * 5  # Each bucket = 5 minutes


def get_verification_summary(opentriage_dir: Path) -> dict[str, Any]:
    """Get summary of pending and completed verifications."""
    state = read_json(opentriage_dir / "state.json")
    pending = state.get("pending_verifications", [])
    return {
        "pending_count": len([p for p in pending if p.get("status") == "pending"]),
        "verified_count": len([p for p in pending if p.get("status") == "verified"]),
        "recurred_count": len([p for p in pending if p.get("status") == "recurred"]),
        "pending": pending,
    }
