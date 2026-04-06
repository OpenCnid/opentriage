"""Remediation engine — applies known remedies within budget (F-OT04, F-AR05)."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from opentriage.config import Config
from opentriage.io.reader import load_fingerprints, load_remediations, read_json, scan_events
from opentriage.io.writer import write_json, write_remediation
from opentriage.remediation.budget import check_budget
from opentriage.remediation.handlers import (
    build_remedy_context,
    execute_callback,
    execute_noop,
    execute_subprocess,
)

log = logging.getLogger(__name__)

# Default skip patterns (G6 / Amendment 5)
DEFAULT_SKIP_PATTERNS = [r"antml:thinking", r"antml:.*artifact"]


def _matches_skip_patterns(f_raw: str, config: Config) -> bool:
    """Check if error matches skip patterns (G6 defense)."""
    patterns = config.remediation.get("skip_patterns") or DEFAULT_SKIP_PATTERNS
    for pattern in patterns:
        try:
            if re.search(pattern, f_raw):
                return True
        except re.error:
            log.warning("Invalid skip pattern: %s", pattern)
    return False


def _check_circuit_breaker(
    slug: str, opentriage_dir: Path, max_consecutive: int = 3, cooldown_hours: int = 24,
) -> tuple[bool, str]:
    """Check if fingerprint is suspended by circuit breaker (F-AR05).

    Returns (can_proceed, reason).
    """
    state = read_json(opentriage_dir / "state.json")
    breakers = state.get("circuit_breakers", {})
    breaker = breakers.get(slug)
    if not breaker:
        return True, ""
    consecutive = breaker.get("consecutive_failures", 0)
    suspended_until = breaker.get("suspended_until")
    if consecutive >= max_consecutive:
        if suspended_until and time.time() < suspended_until:
            return False, f"circuit_breaker_suspended (failures={consecutive}, until={suspended_until})"
    return True, ""


def _update_circuit_breaker(
    slug: str, opentriage_dir: Path, success: bool,
    max_consecutive: int = 3, cooldown_hours: int = 24,
) -> None:
    """Update circuit breaker state after a remediation attempt."""
    state_path = opentriage_dir / "state.json"
    state = read_json(state_path)
    breakers = state.setdefault("circuit_breakers", {})
    breaker = breakers.setdefault(slug, {
        "consecutive_failures": 0,
        "suspended_until": None,
        "last_attempt_ts": None,
    })
    breaker["last_attempt_ts"] = time.time()
    if success:
        breaker["consecutive_failures"] = 0
        breaker["suspended_until"] = None
    else:
        breaker["consecutive_failures"] = breaker.get("consecutive_failures", 0) + 1
        if breaker["consecutive_failures"] >= max_consecutive:
            breaker["suspended_until"] = time.time() + (cooldown_hours * 3600)
            log.warning("Circuit breaker tripped for %s: %d consecutive failures",
                        slug, breaker["consecutive_failures"])
    write_json(state_path, state)


def run_remediation(
    correlations: list[dict[str, Any]],
    config: Config,
    opentriage_dir: Path,
    openlog_dir: Path,
    callback: Callable | None = None,
    project_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Run remediation for eligible correlations. Returns remediation records.

    Supports strategy-based routing (F-AR05):
    - "code-fix": assemble evidence → spawn fix agent
    - "restart": touch restart sentinel
    - "config-change": apply config fix
    - "escalate" / unknown: escalate to human
    """
    fingerprints = load_fingerprints(openlog_dir)
    fp_map = {fp.get("slug", ""): fp for fp in fingerprints}
    handler = config.remediation.get("handler", "subprocess")
    command_template = config.remediation.get("command_template", "")
    timeout = config.remediation.get("timeout_seconds", 300)
    records: list[dict[str, Any]] = []

    # Deduplicate by fingerprint slug within cycle (F-AR05: remediate only first per slug)
    seen: set[tuple[str, str]] = set()
    seen_slugs: set[str] = set()

    # Sort by severity for serial execution (Amendment 6: highest first)
    def _severity_key(c: dict[str, Any]) -> int:
        slug = c.get("matched_fingerprint", "")
        fp = fp_map.get(slug, {})
        severity = fp.get("severity", "")
        order = {"fatal": 0, "recoverable": 1}
        return order.get(severity, 2)

    sorted_correlations = sorted(correlations, key=_severity_key)

    for corr in sorted_correlations:
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
        if not remedy:
            continue
        # Support both string (legacy) and dict (structured F-AR02) remedies
        if isinstance(remedy, str) and not remedy.strip():
            continue

        # Skip patterns check (G6 / Amendment 5)
        f_raw = corr.get("f_raw", "")
        if _matches_skip_patterns(f_raw, config):
            records.append({
                "ts": time.time(),
                "event_ref": corr.get("ref", ""),
                "session_id": corr.get("session_id", ""),
                "fingerprint_slug": slug,
                "action": "skipped",
                "attempt_number": 0,
                "estimated_cost_usd": 0,
                "remedy_applied": "",
                "outcome": "skipped",
                "handler_exit_code": None,
            })
            continue

        # Do not auto-remediate fatal severity (anti-pattern)
        if fp.get("severity") == "fatal":
            records.append({
                "ts": time.time(),
                "event_ref": corr.get("ref", ""),
                "session_id": corr.get("session_id", ""),
                "fingerprint_slug": slug,
                "action": "escalate",
                "attempt_number": 0,
                "estimated_cost_usd": 0,
                "remedy_applied": "",
                "outcome": "escalated_fatal",
                "handler_exit_code": None,
            })
            continue

        event_ref = corr.get("ref", "")
        session_id = corr.get("session_id", "")
        key = (event_ref, session_id)
        if key in seen:
            continue
        seen.add(key)

        # Deduplicate same fingerprint within cycle (F-AR05)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)

        # Circuit breaker check (F-AR05)
        cb_ok, cb_reason = _check_circuit_breaker(slug, opentriage_dir)
        if not cb_ok:
            records.append({
                "ts": time.time(),
                "event_ref": event_ref,
                "session_id": session_id,
                "fingerprint_slug": slug,
                "action": "circuit_breaker",
                "attempt_number": 0,
                "estimated_cost_usd": 0,
                "remedy_applied": "",
                "outcome": "circuit_breaker_suspended",
                "handler_exit_code": None,
                "budget_reason": cb_reason,
            })
            continue

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

        # Strategy-based routing (F-AR05)
        strategy = remedy.get("strategy", "escalate") if isinstance(remedy, dict) else "escalate"

        if strategy == "code-fix" and handler == "agent":
            # F-AR04: Spawn fix agent with evidence bundle
            from opentriage.remediation.evidence import assemble_evidence, write_evidence_bundle
            from opentriage.remediation.agent_handler import spawn_fix_agent

            attempt_id = f"rem-{int(time.time())}-{slug[:20]}"
            evidence = assemble_evidence(
                corr, openlog_dir, opentriage_dir, attempt_id, project_dir,
            )
            write_evidence_bundle(opentriage_dir, evidence)
            agent_result = spawn_fix_agent(evidence, config.remediation, project_dir)

            outcome = agent_result.status
            exit_code = agent_result.exit_code
            is_success = outcome == "fixed"
            _update_circuit_breaker(slug, opentriage_dir, is_success)

            record = {
                "ts": time.time(),
                "event_ref": event_ref,
                "session_id": session_id,
                "fingerprint_slug": slug,
                "action": "agent",
                "attempt_number": attempt_num,
                "estimated_cost_usd": agent_result.duration_seconds * 0.01,  # ~$0.01/sec estimate
                "remedy_applied": (remedy.get("description", "")[:200] if isinstance(remedy, dict) else str(remedy)[:200]),
                "outcome": outcome,
                "handler_exit_code": exit_code,
                "attempt_id": attempt_id,
                "commit_sha": agent_result.commit_sha,
                "files_changed": agent_result.files_changed,
                "verification_failures": agent_result.verification_failures,
            }
        elif strategy == "restart":
            # Touch restart sentinel
            sentinel = (project_dir or Path(".")) / ".opentriage" / "restart_requested"
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text(f"{slug}\n{time.time()}\n")
            exit_code = 0
            outcome = "restart_requested"
            record = {
                "ts": time.time(),
                "event_ref": event_ref,
                "session_id": session_id,
                "fingerprint_slug": slug,
                "action": "restart",
                "attempt_number": attempt_num,
                "estimated_cost_usd": 0,
                "remedy_applied": (remedy.get("description", "")[:200] if isinstance(remedy, dict) else str(remedy)[:200]),
                "outcome": outcome,
                "handler_exit_code": exit_code,
            }
        elif strategy in ("escalate", "config-change") or (strategy == "code-fix" and handler != "agent"):
            # Fallback: use existing handler infrastructure
            remedy_context = build_remedy_context(corr, fp)

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
                "estimated_cost_usd": 0.15,
                "remedy_applied": (remedy.get("description", "")[:200] if isinstance(remedy, dict) else str(remedy)[:200]),
                "outcome": outcome,
                "handler_exit_code": exit_code,
            }
        else:
            # Unknown strategy — escalate
            record = {
                "ts": time.time(),
                "event_ref": event_ref,
                "session_id": session_id,
                "fingerprint_slug": slug,
                "action": "escalate",
                "attempt_number": attempt_num,
                "estimated_cost_usd": 0,
                "remedy_applied": (remedy.get("description", "")[:200] if isinstance(remedy, dict) else str(remedy)[:200]),
                "outcome": "escalated",
                "handler_exit_code": None,
            }

        write_remediation(opentriage_dir, record)
        records.append(record)

        # F-AR06: Record pending verification for successful fixes
        record_pending_verification(opentriage_dir, record)

    return records


def record_pending_verification(
    opentriage_dir: Path,
    record: dict[str, Any],
) -> None:
    """If a remediation succeeded, record it for recurrence verification (F-AR06)."""
    if record.get("outcome") != "fixed":
        return
    from opentriage.remediation.verification import add_pending_verification
    add_pending_verification(
        opentriage_dir,
        fingerprint_slug=record.get("fingerprint_slug", ""),
        attempt_id=record.get("attempt_id", f"rem-{int(record.get('ts', time.time()))}"),
        commit_sha=record.get("commit_sha"),
    )


def track_outcomes(
    config: Config,
    opentriage_dir: Path,
    openlog_dir: Path,
) -> list[dict[str, Any]]:
    """Track outcomes of pending remediations. Returns updated records.

    Also runs F-AR06 recurrence verification for fix-agent results.
    """
    # F-AR06: Check recurrence verifications
    from opentriage.remediation.verification import check_recurrence
    verification_results = check_recurrence(opentriage_dir, openlog_dir)
    for vr in verification_results:
        if vr.get("status") == "recurred":
            slug = vr.get("fingerprint_slug", "")
            _update_circuit_breaker(slug, opentriage_dir, success=False)
            log.warning("Recurrence detected for %s — circuit breaker updated", slug)
        elif vr.get("status") == "verified":
            slug = vr.get("fingerprint_slug", "")
            _update_circuit_breaker(slug, opentriage_dir, success=True)
            log.info("Fix verified for %s — circuit breaker reset", slug)

    # Original outcome tracking for non-agent handlers
    all_rems = load_remediations(opentriage_dir)
    pending = [r for r in all_rems if r.get("outcome") == "pending"]
    if not pending:
        return verification_results

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

    return verification_results + updated
