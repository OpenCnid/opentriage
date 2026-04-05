"""Remediation engine — applies known remedies within budget (F-OT04)."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

from opentriage.config import Config
from opentriage.io.reader import load_fingerprints, load_remediations, scan_events
from opentriage.io.writer import write_remediation
from opentriage.remediation.budget import check_budget
from opentriage.remediation.handlers import (
    build_remedy_context,
    execute_callback,
    execute_noop,
    execute_subprocess,
)

log = logging.getLogger(__name__)


def run_remediation(
    correlations: list[dict[str, Any]],
    config: Config,
    opentriage_dir: Path,
    openlog_dir: Path,
    callback: Callable | None = None,
) -> list[dict[str, Any]]:
    """Run remediation for eligible correlations. Returns remediation records."""
    fingerprints = load_fingerprints(openlog_dir)
    fp_map = {fp.get("slug", ""): fp for fp in fingerprints}
    handler = config.remediation.get("handler", "subprocess")
    command_template = config.remediation.get("command_template", "")
    timeout = config.remediation.get("timeout_seconds", 300)
    records: list[dict[str, Any]] = []

    # Deduplicate by event_ref + session_id
    seen: set[tuple[str, str]] = set()

    for corr in correlations:
        cls = corr.get("classification")
        conf = corr.get("confidence")
        slug = corr.get("matched_fingerprint")

        # Only known-pattern with high/medium confidence
        if cls != "known-pattern" or conf not in ("high", "medium"):
            continue
        if not slug:
            continue

        fp = fp_map.get(slug)
        if not fp:
            continue

        remedy = fp.get("remedy")
        if not remedy or not remedy.strip():
            continue

        event_ref = corr.get("ref", "")
        session_id = corr.get("session_id", "")
        key = (event_ref, session_id)
        if key in seen:
            continue
        seen.add(key)

        # Budget check
        event = {"ref": event_ref, "session_id": session_id}
        ok, reason = check_budget(event, config.budget, opentriage_dir)
        if not ok:
            records.append({
                "ts": time.time(),
                "event_ref": event_ref,
                "session_id": session_id,
                "fingerprint_slug": slug,
                "action": "budget_exceeded",
                "attempt_number": -1,
                "estimated_cost_usd": 0,
                "remedy_applied": "",
                "outcome": "budget_exceeded",
                "handler_exit_code": None,
                "budget_reason": reason,
            })
            continue

        # Count previous attempts
        all_rems = load_remediations(opentriage_dir)
        attempt_num = sum(
            1 for r in all_rems
            if r.get("event_ref") == event_ref and r.get("session_id") == session_id
        ) + 1

        remedy_context = build_remedy_context(corr, fp)

        # Execute handler
        if handler == "noop":
            exit_code, output = execute_noop(corr, fp, remedy_context)
        elif handler == "callback" and callback:
            exit_code, output = execute_callback(callback, corr, fp, remedy_context)
        elif handler == "subprocess" and command_template:
            exit_code, output = execute_subprocess(
                command_template, corr, fp, remedy_context, timeout
            )
        else:
            exit_code, output = execute_noop(corr, fp, remedy_context)

        outcome = "pending"
        if exit_code == -1:
            outcome = "spawn_failed"
        elif exit_code == -2:
            outcome = "timeout"

        record = {
            "ts": time.time(),
            "event_ref": event_ref,
            "session_id": session_id,
            "fingerprint_slug": slug,
            "action": handler,
            "attempt_number": attempt_num,
            "estimated_cost_usd": 0.15,  # Default estimate
            "remedy_applied": remedy[:200],
            "outcome": outcome,
            "handler_exit_code": exit_code,
        }

        write_remediation(opentriage_dir, record)
        records.append(record)

    return records


def track_outcomes(
    config: Config,
    opentriage_dir: Path,
    openlog_dir: Path,
) -> list[dict[str, Any]]:
    """Track outcomes of pending remediations. Returns updated records."""
    all_rems = load_remediations(opentriage_dir)
    pending = [r for r in all_rems if r.get("outcome") == "pending"]
    if not pending:
        return []

    now = time.time()
    updated: list[dict[str, Any]] = []

    for rem in pending:
        rem_ts = rem.get("ts", 0)
        slug = rem.get("fingerprint_slug", "")
        session_id = rem.get("session_id", "")

        # Scan for events after remediation
        subsequent = scan_events(openlog_dir)
        post_events = [
            e for e in subsequent
            if e.get("ts", 0) > rem_ts
            and (e.get("session_id") == session_id or e.get("ref") == rem.get("event_ref"))
        ]

        if not post_events:
            if now - rem_ts >= 86400:  # 24 hours
                rem["outcome"] = "no_result"
                updated.append(rem)
            continue

        # Check for recurrence of same pattern
        from opentriage.triage.matcher import match_event
        from opentriage.io.reader import load_fingerprints as _load_fps
        fps = _load_fps(openlog_dir)

        recurred = False
        resolved = False
        for e in post_events:
            if e.get("kind") == "error" and e.get("f_raw"):
                result = match_event(e["f_raw"], fps)
                if result.matched and result.fingerprint_slug == slug:
                    recurred = True
                    break
            if e.get("kind") == "complete":
                resolved = True

        if recurred:
            rem["outcome"] = "failure"
        elif resolved:
            rem["outcome"] = "success"
        elif now - rem_ts >= 86400:
            rem["outcome"] = "no_result"
        else:
            continue  # Still pending

        updated.append(rem)

    return updated
