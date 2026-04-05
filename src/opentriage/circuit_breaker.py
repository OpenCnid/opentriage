"""Circuit breaker state machine (F-OT03)."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from opentriage.io.reader import read_json
from opentriage.io.writer import write_state

log = logging.getLogger(__name__)

STATES = ("full-autonomy", "classify-only", "observe-only", "suspended")
# Higher rank = more restrictive
STATE_RANK = {s: i for i, s in enumerate(STATES)}

PERMISSIONS: dict[str, dict[str, bool]] = {
    "full-autonomy": {"classify": True, "remediate": True, "escalate": True, "draft": True},
    "classify-only": {"classify": True, "remediate": False, "escalate": True, "draft": True},
    "observe-only": {"classify": True, "remediate": False, "escalate": False, "draft": False},
    "suspended": {"classify": False, "remediate": False, "escalate": False, "draft": False},
}

DEFAULT_STATE: dict[str, Any] = {
    "circuit_breaker": "full-autonomy",
    "last_triage_run": None,
    "last_health_run": None,
    "demotion_history": [],
    "rolling_remediation_success_rate": None,
    "rolling_override_rate": None,
    "net_remediation_effect": None,
    "total_remediations": 0,
    "total_escalations": 0,
    "consecutive_provider_errors": 0,
    "human_approved_promotion": False,
    "version": "1.0",
}


def load_state(opentriage_dir: Path) -> dict[str, Any]:
    """Load state.json, defaulting to suspended on corruption."""
    state_path = opentriage_dir / "state.json"
    state = read_json(state_path)
    if not state or "circuit_breaker" not in state:
        log.warning("State file missing or corrupted — defaulting to suspended")
        state = dict(DEFAULT_STATE)
        state["circuit_breaker"] = "suspended"
        write_state(opentriage_dir, state)
        return state
    # Validate circuit_breaker value
    if state["circuit_breaker"] not in STATES:
        log.warning("Invalid circuit breaker state '%s' — defaulting to suspended", state["circuit_breaker"])
        state["circuit_breaker"] = "suspended"
    # Fill missing keys from defaults
    for k, v in DEFAULT_STATE.items():
        if k not in state:
            state[k] = v
    return state


def can(state: dict[str, Any], action: str) -> bool:
    """Check if an action is permitted in the current circuit breaker state."""
    cb = state.get("circuit_breaker", "suspended")
    perms = PERMISSIONS.get(cb, PERMISSIONS["suspended"])
    if action == "escalate" and cb == "observe-only":
        return True  # critical-only escalations allowed
    return perms.get(action, False)


def evaluate_demotions(state: dict[str, Any], config_cb: dict[str, Any]) -> str | None:
    """Check if any automatic demotion should fire. Returns new state or None."""
    current = state["circuit_breaker"]
    if current == "suspended":
        return None

    candidates: list[str] = []

    # Provider errors → suspended
    if state.get("consecutive_provider_errors", 0) >= 3:
        candidates.append("suspended")

    # Need enough data for metric-based demotions
    min_resolved = config_cb.get("min_resolved_for_evaluation", 5)
    floor = config_cb.get("classification_accuracy_floor", 0.70)

    rate = state.get("rolling_remediation_success_rate")
    net = state.get("net_remediation_effect")

    if rate is not None and state.get("total_remediations", 0) >= min_resolved:
        # full-autonomy → classify-only on low success rate
        if current == "full-autonomy" and rate < floor:
            candidates.append("classify-only")

    if net is not None and state.get("total_remediations", 0) >= min_resolved:
        # full-autonomy → observe-only on negative net effect
        if current == "full-autonomy" and net < 0:
            candidates.append("observe-only")

    if not candidates:
        return None

    # Apply most restrictive
    candidates.sort(key=lambda s: STATE_RANK.get(s, 0), reverse=True)
    new_state = candidates[0]
    if STATE_RANK.get(new_state, 0) <= STATE_RANK.get(current, 0):
        return None  # Can't demote to same or less restrictive
    return new_state


def evaluate_promotions(state: dict[str, Any], config_cb: dict[str, Any]) -> str | None:
    """Check if a human-approved promotion should fire. Returns new state or None."""
    if not state.get("human_approved_promotion", False):
        return None

    current = state["circuit_breaker"]
    recovery = config_cb.get("recovery_threshold", 0.80)
    rate = state.get("rolling_remediation_success_rate")

    if current == "classify-only" and rate is not None and rate > recovery:
        return "full-autonomy"
    if current == "observe-only":
        return "classify-only"
    if current == "suspended":
        return "observe-only"

    return None


def transition(state: dict[str, Any], new_cb: str, reason: str) -> dict[str, Any]:
    """Apply a state transition, updating demotion_history."""
    old = state["circuit_breaker"]
    state["circuit_breaker"] = new_cb
    entry = {"from": old, "to": new_cb, "reason": reason, "ts": time.time()}
    history = state.get("demotion_history", [])
    history.append(entry)
    state["demotion_history"] = history
    # Reset promotion flag on any transition
    if "human_approved_promotion" in state:
        state["human_approved_promotion"] = False
    return state


def update_metrics(
    state: dict[str, Any],
    resolved_outcomes: list[str],
    override_count: int = 0,
    standard_tier_calls: int = 0,
) -> dict[str, Any]:
    """Recompute rolling metrics from resolved remediation outcomes."""
    if not resolved_outcomes:
        return state

    successes = resolved_outcomes.count("success")
    failures = resolved_outcomes.count("failure") + resolved_outcomes.count("no_result")
    total = successes + failures
    if total > 0:
        state["rolling_remediation_success_rate"] = round(successes / total, 4)
        state["net_remediation_effect"] = round((successes - failures) / total, 4)
        state["total_remediations"] = state.get("total_remediations", 0) + len(resolved_outcomes)

    if standard_tier_calls > 0:
        state["rolling_override_rate"] = round(override_count / standard_tier_calls, 4)

    return state


def run_circuit_breaker(
    state: dict[str, Any],
    config_cb: dict[str, Any],
    opentriage_dir: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Evaluate transitions and return (updated_state, transition_alerts).

    Alerts are dicts suitable for the escalation system.
    """
    alerts: list[dict[str, Any]] = []

    # Check demotions first
    new_state = evaluate_demotions(state, config_cb)
    if new_state:
        reason = _demotion_reason(state, config_cb)
        state = transition(state, new_state, reason)
        alerts.append({
            "severity": "critical",
            "type": "circuit_breaker_change",
            "title": f"Circuit breaker: {state['demotion_history'][-1]['from']} → {new_state}",
            "body": reason,
            "context": {"old_state": state["demotion_history"][-1]["from"], "new_state": new_state},
            "action_needed": "Review metrics. Use 'opentriage promote' to restore authority after fixing issues.",
            "ts": time.time(),
        })
    else:
        # Check promotions only if no demotion
        new_state = evaluate_promotions(state, config_cb)
        if new_state:
            old = state["circuit_breaker"]
            reason = f"Human-approved promotion. Metrics qualify: success_rate={state.get('rolling_remediation_success_rate')}"
            state = transition(state, new_state, reason)
            alerts.append({
                "severity": "info",
                "type": "circuit_breaker_change",
                "title": f"Circuit breaker promoted: {old} → {new_state}",
                "body": reason,
                "context": {"old_state": old, "new_state": new_state},
                "action_needed": "No action needed. Monitoring continues.",
                "ts": time.time(),
            })

    write_state(opentriage_dir, state)
    return state, alerts


def _demotion_reason(state: dict[str, Any], config_cb: dict[str, Any]) -> str:
    parts: list[str] = []
    if state.get("consecutive_provider_errors", 0) >= 3:
        parts.append(f"consecutive_provider_errors={state['consecutive_provider_errors']}")
    rate = state.get("rolling_remediation_success_rate")
    floor = config_cb.get("classification_accuracy_floor", 0.70)
    if rate is not None and rate < floor:
        parts.append(f"remediation_success_rate={rate} < floor={floor}")
    net = state.get("net_remediation_effect")
    if net is not None and net < 0:
        parts.append(f"net_remediation_effect={net} < 0")
    return "; ".join(parts) or "unknown"
