"""Health monitor — metrics computation (F-OT07)."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from opentriage.config import Config
from opentriage.io.reader import (
    load_correlations,
    load_escalations,
    load_remediations,
    read_json,
)
from opentriage.io.writer import write_metrics

log = logging.getLogger(__name__)


def run_health(
    config: Config,
    opentriage_dir: Path,
    days: int = 7,
    today_only: bool = False,
) -> dict[str, Any]:
    """Compute health metrics for the requested period."""
    now = datetime.now(timezone.utc)

    if today_only:
        dates = [now.strftime("%Y-%m-%d")]
    else:
        dates = [
            (now - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(days - 1, -1, -1)
        ]

    daily_metrics: list[dict[str, Any]] = []
    for date_str in dates:
        m = _compute_daily(date_str, config, opentriage_dir)
        write_metrics(opentriage_dir, date_str, m)
        daily_metrics.append(m)

    # Summary
    summary = _summarize(daily_metrics, dates)
    return summary


def _compute_daily(date_str: str, config: Config, opentriage_dir: Path) -> dict[str, Any]:
    """Compute metrics for a single day."""
    correlations = load_correlations(opentriage_dir, date_str)
    remediations = load_remediations(opentriage_dir, date_str)
    escalations = load_escalations(opentriage_dir)
    day_escalations = [e for e in escalations if _ts_to_date(e.get("ts", 0)) == date_str]

    # Classifications
    cls_counts = Counter(c.get("classification") for c in correlations)
    tier_counts = Counter(c.get("tier") for c in correlations)
    overrides = sum(1 for c in correlations if c.get("overridden_by"))
    standard_calls = sum(1 for c in correlations if c.get("tier") == "confirmation_path")

    # Remediations
    outcomes = Counter(r.get("outcome") for r in remediations)
    total_resolved = outcomes.get("success", 0) + outcomes.get("failure", 0) + outcomes.get("no_result", 0)
    success_rate = outcomes.get("success", 0) / total_resolved if total_resolved > 0 else None

    # Costs
    rem_cost = sum(r.get("estimated_cost_usd", 0) for r in remediations)

    # State
    state = read_json(opentriage_dir / "state.json")
    pending_drafts = len(list((opentriage_dir / "drafts").glob("*.json"))) if (opentriage_dir / "drafts").exists() else 0

    return {
        "date": date_str,
        "events": {
            "total_scanned": len(correlations),
            "errors_found": len(correlations),
            "correlated": len(correlations),
            "uncorrelated_remaining": 0,
        },
        "classifications": {
            "known_pattern_fast_path": tier_counts.get("fast_path", 0),
            "known_pattern_llm": tier_counts.get("slow_path", 0),
            "novel": cls_counts.get("novel", 0),
            "transient": cls_counts.get("transient", 0),
            "deferred": cls_counts.get("deferred", 0),
            "override_count": overrides,
            "override_rate": round(overrides / standard_calls, 2) if standard_calls > 0 else 0,
        },
        "remediations": {
            "attempted": len(remediations),
            "succeeded": outcomes.get("success", 0),
            "failed": outcomes.get("failure", 0),
            "no_result": outcomes.get("no_result", 0),
            "escalated_budget": outcomes.get("budget_exceeded", 0),
            "success_rate": round(success_rate, 2) if success_rate is not None else None,
        },
        "cost": {
            "cheap_tier_usd": 0,  # Tracked by provider in real use
            "standard_tier_usd": 0,
            "expensive_tier_usd": 0,
            "remediation_subprocess_usd": round(rem_cost, 2),
            "total_usd": round(rem_cost, 2),
        },
        "system": {
            "circuit_breaker_state": state.get("circuit_breaker", "unknown"),
            "state_transitions": len([
                h for h in state.get("demotion_history", [])
                if _ts_to_date(h.get("ts", 0)) == date_str
            ]),
            "pending_drafts": pending_drafts,
            "triage_cycles_run": 0,  # Would need separate tracking
            "escalations_sent": len(day_escalations),
        },
    }


def _summarize(daily_metrics: list[dict[str, Any]], dates: list[str]) -> dict[str, Any]:
    """Summarize daily metrics into a period report."""
    total_events = sum(m["events"]["total_scanned"] for m in daily_metrics)
    total_novel = sum(m["classifications"]["novel"] for m in daily_metrics)
    total_rems = sum(m["remediations"]["attempted"] for m in daily_metrics)
    total_success = sum(m["remediations"]["succeeded"] for m in daily_metrics)
    total_cost = sum(m["cost"]["total_usd"] for m in daily_metrics)

    return {
        "period": f"{dates[0]} to {dates[-1]}" if len(dates) > 1 else dates[0],
        "days": len(dates),
        "total_events": total_events,
        "total_novel": total_novel,
        "total_remediations": total_rems,
        "total_successes": total_success,
        "total_cost_usd": round(total_cost, 2),
        "daily": daily_metrics,
    }


def _ts_to_date(ts: float) -> str:
    """Convert unix timestamp to date string."""
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
