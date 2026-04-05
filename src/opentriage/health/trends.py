"""Trend detection for health monitoring (F-OT07)."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from opentriage.config import Config
from opentriage.io.reader import load_correlations, load_remediations, read_json

log = logging.getLogger(__name__)


def detect_trends(
    config: Config,
    opentriage_dir: Path,
    today_metrics: dict[str, Any],
) -> list[dict[str, Any]]:
    """Detect trends by comparing today vs historical. Returns alert dicts."""
    alerts: list[dict[str, Any]] = []
    health_cfg = config.health
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    # Load historical metrics for comparison (last 7 days excluding today)
    metrics_dir = opentriage_dir / "metrics"
    historical: list[dict[str, Any]] = []
    for i in range(1, 8):
        date_str = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        path = metrics_dir / f"{date_str}.json"
        if path.exists():
            m = read_json(path)
            if m:
                historical.append(m)

    if len(historical) < 3:
        log.info("Trend detection requires >= 3 days of history (%d available)", len(historical))
        return alerts

    # 1. Pattern frequency spike
    today_corrs = load_correlations(opentriage_dir, today_str)
    today_slugs = Counter(c.get("matched_fingerprint") for c in today_corrs if c.get("matched_fingerprint"))
    spike_threshold = health_cfg.get("trend_pattern_spike_threshold", 3)

    for slug, count in today_slugs.items():
        hist_counts = []
        for h in historical:
            # Count from historical day's correlations
            date = h.get("date", "")
            day_corrs = load_correlations(opentriage_dir, date)
            day_count = sum(1 for c in day_corrs if c.get("matched_fingerprint") == slug)
            hist_counts.append(day_count)
        avg = sum(hist_counts) / len(hist_counts) if hist_counts else 0
        if count >= spike_threshold and avg <= 1:
            alerts.append(_trend_alert(
                f"Pattern spike: {slug}",
                f"{slug} appeared {count} times today vs {avg:.1f} daily average",
                {"slug": slug, "today": count, "avg": round(avg, 1)},
            ))

    # 2. Remediation failure rate per fingerprint
    failure_threshold = health_cfg.get("trend_remediation_failure_rate", 0.50)
    today_rems = load_remediations(opentriage_dir, today_str)
    slug_rems: dict[str, list[str]] = {}
    for r in today_rems:
        s = r.get("fingerprint_slug", "")
        slug_rems.setdefault(s, []).append(r.get("outcome", "pending"))
    for slug, outcomes in slug_rems.items():
        resolved = [o for o in outcomes if o in ("success", "failure", "no_result")]
        if resolved:
            failures = sum(1 for o in resolved if o != "success")
            rate = failures / len(resolved)
            if rate > failure_threshold:
                alerts.append(_trend_alert(
                    f"High failure rate: {slug}",
                    f"Remediation failure rate for {slug}: {rate:.0%} (threshold: {failure_threshold:.0%})",
                    {"slug": slug, "failure_rate": round(rate, 2)},
                ))

    # 3. Novel rate
    novel_threshold = health_cfg.get("trend_novel_rate", 0.40)
    total_errors = today_metrics.get("events", {}).get("errors_found", 0)
    novel_count = today_metrics.get("classifications", {}).get("novel", 0)
    if total_errors > 0:
        novel_rate = novel_count / total_errors
        if novel_rate > novel_threshold:
            alerts.append(_trend_alert(
                "High novel rate",
                f"Novel rate: {novel_rate:.0%} ({novel_count}/{total_errors}). Fingerprint catalog may be stale.",
                {"novel_rate": round(novel_rate, 2), "novel_count": novel_count, "total": total_errors},
            ))

    # 4. Override rate
    override_threshold = health_cfg.get("trend_override_rate", 0.30)
    override_rate = today_metrics.get("classifications", {}).get("override_rate", 0)
    if override_rate > override_threshold:
        alerts.append(_trend_alert(
            "High override rate",
            f"Override rate: {override_rate:.0%}. Cheap-tier model or prompt needs tuning.",
            {"override_rate": override_rate},
        ))

    # 5. Daily cost warning
    cost_warning = health_cfg.get("trend_daily_cost_warning_usd", 10.0)
    daily_cost = today_metrics.get("cost", {}).get("total_usd", 0)
    if daily_cost > cost_warning:
        alerts.append(_trend_alert(
            "Daily cost warning",
            f"Daily cost: ${daily_cost:.2f} (warning at ${cost_warning:.2f})",
            {"daily_cost_usd": daily_cost, "threshold": cost_warning},
        ))

    # 6. Pending drafts
    max_drafts = health_cfg.get("trend_pending_drafts_max", 5)
    pending = today_metrics.get("system", {}).get("pending_drafts", 0)
    if pending > max_drafts:
        alerts.append(_trend_alert(
            "Pending drafts backlog",
            f"{pending} unreviewed drafts (max: {max_drafts}). Human review needed.",
            {"pending_drafts": pending, "max": max_drafts},
        ))

    return alerts


def _trend_alert(title: str, body: str, context: dict[str, Any]) -> dict[str, Any]:
    """Build a trend alert dict."""
    import time
    return {
        "severity": "high",
        "type": "trend_alert",
        "title": title,
        "body": body,
        "context": context,
        "action_needed": "Review metrics and take corrective action.",
        "ts": time.time(),
    }
