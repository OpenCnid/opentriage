"""Budget tracking for remediation (F-OT04)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opentriage.io.reader import load_remediations


def check_budget(
    event: dict[str, Any],
    config_budget: dict[str, Any],
    opentriage_dir: Path,
) -> tuple[bool, str]:
    """Check all 4 budget limits. Returns (ok, reason)."""
    event_ref = event.get("ref", "")
    session_id = event.get("session_id", "")
    max_retries = config_budget.get("max_retries_per_event", 2)
    max_event_cost = config_budget.get("max_cost_per_event_usd", 5.0)
    max_daily = config_budget.get("max_daily_cost_usd", 20.0)
    max_weekly = config_budget.get("max_weekly_cost_usd", 50.0)

    all_remediations = load_remediations(opentriage_dir)
    now = time.time()

    # 1. Per-event retry count
    event_attempts = [
        r for r in all_remediations
        if r.get("event_ref") == event_ref and r.get("session_id") == session_id
    ]
    if len(event_attempts) >= max_retries:
        return False, f"max_retries_per_event exceeded ({len(event_attempts)}/{max_retries})"

    # 2. Per-event cost
    event_cost = sum(r.get("estimated_cost_usd", 0) for r in event_attempts)
    if event_cost >= max_event_cost:
        return False, f"max_cost_per_event_usd exceeded (${event_cost:.2f}/${max_event_cost:.2f})"

    # 3. Daily cost (UTC midnight to now)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_ts = today_start.timestamp()
    daily_cost = sum(
        r.get("estimated_cost_usd", 0) for r in all_remediations
        if r.get("ts", 0) >= today_ts
    )
    if daily_cost >= max_daily:
        return False, f"max_daily_cost_usd exceeded (${daily_cost:.2f}/${max_daily:.2f})"

    # 4. Weekly cost (Monday 00:00 UTC to now)
    today = datetime.now(timezone.utc)
    monday = today - __import__("datetime").timedelta(days=today.weekday())
    week_start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ts = week_start.timestamp()
    weekly_cost = sum(
        r.get("estimated_cost_usd", 0) for r in all_remediations
        if r.get("ts", 0) >= week_ts
    )
    if weekly_cost >= max_weekly:
        return False, f"max_weekly_cost_usd exceeded (${weekly_cost:.2f}/${max_weekly:.2f})"

    return True, ""
