"""CLI entry point for OpenTriage."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from opentriage import __version__
from opentriage.config import Config, DEFAULT_CONFIG, resolve_paths


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="opentriage",
        description="OpenTriage — Model-agnostic failure response engine",
    )
    parser.add_argument("--version", action="version", version=f"opentriage {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug output")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress all except errors")
    parser.add_argument("--config", type=str, default=None, help="Config file path")
    parser.add_argument("--openlog-dir", type=str, default=None, help="OpenLog data directory")

    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Initialize .opentriage/ directory")
    p_init.add_argument("--force", action="store_true", help="Reinitialize (resets config)")

    # triage
    p_triage = sub.add_parser("triage", help="Run one triage cycle")
    p_triage.add_argument("--window", type=float, default=None, help="Scan window in hours")
    p_triage.add_argument("--all", action="store_true", help="Scan full history")
    p_triage.add_argument("--dry-run", action="store_true", help="Classify without modifying state")

    # remediate
    p_rem = sub.add_parser("remediate", help="Manually trigger remediation for an event")
    p_rem.add_argument("--event", required=True, help="Event ref")
    p_rem.add_argument("--session", required=True, help="Session ID")

    # status
    sub.add_parser("status", help="Print circuit breaker state and metrics")

    # health
    p_health = sub.add_parser("health", help="Run health monitor")
    p_health.add_argument("--days", type=int, default=7, help="Number of days")
    p_health.add_argument("--today", action="store_true", help="Today only")

    # watch
    p_watch = sub.add_parser("watch", help="Continuous triage mode")
    p_watch.add_argument("--interval", type=int, default=120, help="Seconds between cycles")

    # promote
    sub.add_parser("promote", help="Approve circuit breaker promotion")

    # config
    p_cfg = sub.add_parser("config", help="View or set config values")
    p_cfg.add_argument("key", nargs="?", help="Config key (e.g. provider.backend)")
    p_cfg.add_argument("value", nargs="?", help="Value to set")

    # drafts
    p_drafts = sub.add_parser("drafts", help="List pending draft fingerprints")
    p_drafts.add_argument("--json", action="store_true", dest="json_output", help="Output raw JSON")

    # approve
    p_approve = sub.add_parser("approve", help="Approve a draft fingerprint")
    p_approve.add_argument("slug", help="Draft slug to approve")
    p_approve.add_argument("--comment", type=str, default="", help="Approval comment")

    # reject
    p_reject = sub.add_parser("reject", help="Reject a draft fingerprint")
    p_reject.add_argument("slug", help="Draft slug to reject")
    p_reject.add_argument("--reason", type=str, default="", help="Rejection reason")

    # escalations
    p_esc = sub.add_parser("escalations", help="Show recent escalations")
    p_esc.add_argument("--last", type=int, default=20, help="Number of escalations to show")
    p_esc.add_argument("--json", action="store_true", dest="json_output", help="Output raw JSONL")

    # validate
    sub.add_parser("validate", help="Validate the installation")

    # calibrate
    p_cal = sub.add_parser("calibrate", help="Run a calibration check")
    p_cal.add_argument("--events", type=int, default=10, help="Number of events to check")

    # revert
    p_rev = sub.add_parser("revert", help="Revert a remediation")
    p_rev.add_argument("--remediation-id", required=True, help="Remediation ID to revert")

    # cleanup
    p_clean = sub.add_parser("cleanup", help="Clean up old data files")
    p_clean.add_argument("--older-than", type=int, default=30, help="Days threshold")
    p_clean.add_argument("--dry-run", action="store_true", help="List without deleting")

    args = parser.parse_args(argv)

    # Logging
    level = logging.DEBUG if args.verbose else (logging.ERROR if args.quiet else logging.INFO)
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Resolve paths
    config_path = Path(args.config) if args.config else None
    openlog_override = Path(args.openlog_dir) if args.openlog_dir else None

    if args.command == "init":
        _cmd_init(config_path, openlog_override, force=args.force)
    elif args.command == "config":
        _cmd_config(config_path, openlog_override, args.key, args.value)
    elif args.command == "validate":
        _cmd_validate(config_path, openlog_override)
    else:
        # All other commands require init
        ot_dir, ol_dir = resolve_paths(openlog_dir=openlog_override)
        if config_path:
            ot_dir = config_path.parent
        if not ot_dir.exists():
            print("Run 'opentriage init' first.", file=sys.stderr)
            sys.exit(1)

        cfg = Config.load(ot_dir / "config.toml")

        if args.command == "triage":
            _cmd_triage(cfg, ot_dir, ol_dir, args)
        elif args.command == "remediate":
            _cmd_remediate(cfg, ot_dir, ol_dir, args)
        elif args.command == "status":
            _cmd_status(ot_dir)
        elif args.command == "health":
            _cmd_health(cfg, ot_dir, args)
        elif args.command == "watch":
            _cmd_watch(cfg, ot_dir, ol_dir, args)
        elif args.command == "promote":
            _cmd_promote(ot_dir, cfg)
        elif args.command == "drafts":
            _cmd_drafts(ot_dir, args)
        elif args.command == "approve":
            _cmd_approve(ot_dir, ol_dir, args)
        elif args.command == "reject":
            _cmd_reject(ot_dir, args)
        elif args.command == "escalations":
            _cmd_escalations(ot_dir, args)
        elif args.command == "calibrate":
            _cmd_calibrate(ot_dir, ol_dir, args)
        elif args.command == "revert":
            _cmd_revert(ot_dir, args)
        elif args.command == "cleanup":
            _cmd_cleanup(ot_dir, args)


def _cmd_init(config_path: Path | None, openlog_override: Path | None, force: bool = False) -> None:
    ot_dir, ol_dir = resolve_paths(openlog_dir=openlog_override)
    if config_path:
        ot_dir = config_path.parent

    if ot_dir.exists() and not force:
        print("Already initialized.")
        return

    # Create directories
    ot_dir.mkdir(parents=True, exist_ok=True)
    for subdir in ("correlations", "remediations", "drafts", "metrics"):
        (ot_dir / subdir).mkdir(exist_ok=True)

    # Write default config
    cfg = Config()
    cfg.save(ot_dir / "config.toml")

    # Write initial state
    from opentriage.circuit_breaker import DEFAULT_STATE
    from opentriage.io.writer import write_state
    write_state(ot_dir, dict(DEFAULT_STATE))

    print(f"Initialized .opentriage/ at {ot_dir}")

    if not ol_dir.exists():
        print("WARNING: OpenLog directory not found. Install openlog-agent first.")


def _cmd_triage(cfg: Config, ot_dir: Path, ol_dir: Path, args: Any) -> None:
    # Acquire lock
    lock = _acquire_lock(ot_dir)
    if lock is None:
        sys.exit(1)

    try:
        provider = _build_provider(cfg)

        from opentriage.triage.engine import run_triage
        result = run_triage(
            cfg, provider, ot_dir, ol_dir,
            window_hours=args.window,
            scan_all=args.all,
            dry_run=args.dry_run,
        )

        if result.get("status") == "skipped":
            print(f"Triage skipped: {result.get('reason', 'unknown')}")
            return

        stats = result.get("stats", {})
        print(f"Triage complete: {result.get('events_processed', 0)} events processed")
        print(f"  Fast path: {stats.get('fast_path', 0)}, LLM: {stats.get('slow_path', 0)}, "
              f"Novel: {stats.get('novel', 0)}, Transient: {stats.get('transient', 0)}")

        if result.get("backlog", 0) > 0:
            print(f"  Backlog: {result['backlog']} events deferred")

        # Run remediation if full-autonomy and not dry-run
        if not args.dry_run:
            from opentriage.circuit_breaker import load_state, can
            state = load_state(ot_dir)
            correlations = result.get("correlations", [])

            if can(state, "remediate"):
                from opentriage.remediation.engine import run_remediation
                rems = run_remediation(correlations, cfg, ot_dir, ol_dir)
                if rems:
                    print(f"  Remediations: {len(rems)}")

                # Budget-exceeded → escalate
                from opentriage.escalation.router import EscalationBatcher
                batcher = EscalationBatcher(cfg.escalation, ot_dir)
                for r in rems:
                    if r.get("outcome") == "budget_exceeded":
                        batcher.escalate({
                            "severity": "high",
                            "type": "budget_exceeded",
                            "title": f"Budget exceeded for {r.get('fingerprint_slug', '?')}",
                            "body": r.get("budget_reason", ""),
                            "context": r,
                            "action_needed": "Review budget limits or wait for reset.",
                            "ts": time.time(),
                        })

            # Novel synthesis
            if can(state, "draft"):
                novel = [c for c in correlations if c.get("classification") == "novel"]
                if novel:
                    from opentriage.synthesis.drafter import run_synthesis
                    drafts = run_synthesis(novel, _build_provider(cfg), ot_dir, ol_dir)
                    if drafts:
                        print(f"  Drafts created: {len(drafts)}")

            # Escalate novel patterns
            if can(state, "escalate"):
                from opentriage.escalation.router import EscalationBatcher
                batcher = EscalationBatcher(cfg.escalation, ot_dir)
                for c in correlations:
                    if c.get("classification") == "novel" and c.get("confidence") in ("high", "medium"):
                        batcher.escalate({
                            "severity": "high",
                            "type": "novel_pattern",
                            "title": f"Novel failure: {c.get('f_raw', '?')[:60]}",
                            "body": c.get("reasoning", "No reasoning provided"),
                            "context": {"ref": c.get("ref"), "session_id": c.get("session_id")},
                            "action_needed": "Review draft in .opentriage/drafts/",
                            "ts": time.time(),
                        })
                batcher.flush()

            # Outcome tracking
            from opentriage.remediation.engine import track_outcomes
            track_outcomes(cfg, ot_dir, ol_dir)

            # Circuit breaker evaluation
            from opentriage.circuit_breaker import run_circuit_breaker
            state, cb_alerts = run_circuit_breaker(state, cfg.circuit_breaker, ot_dir)
            if cb_alerts:
                from opentriage.escalation.router import send_alert
                for alert in cb_alerts:
                    send_alert(alert, cfg.escalation, ot_dir)

        # Check for backlog escalation
        if result.get("backlog", 0) > 100:
            from opentriage.escalation.router import send_alert
            send_alert({
                "severity": "high",
                "type": "triage_backlog",
                "title": f"Triage backlog: {result['backlog']} events pending",
                "body": "Backlog exceeds 100 events. Consider increasing cycle frequency.",
                "context": {"pending": result["backlog"]},
                "action_needed": "Increase triage frequency or investigate event volume.",
                "ts": time.time(),
            }, cfg.escalation, ot_dir)

        exit_code = 0
        # Exit code 2 if critical issues found
        if stats.get("novel", 0) > 0 or any(
            c.get("classification") == "novel" and c.get("confidence") == "high"
            for c in result.get("correlations", [])
        ):
            exit_code = 2

        sys.exit(exit_code)

    finally:
        _release_lock(ot_dir)


def _cmd_remediate(cfg: Config, ot_dir: Path, ol_dir: Path, args: Any) -> None:
    from opentriage.io.reader import load_correlations
    from opentriage.circuit_breaker import load_state, can

    state = load_state(ot_dir)
    if not can(state, "remediate"):
        print(f"Remediation not allowed in state: {state.get('circuit_breaker')}", file=sys.stderr)
        sys.exit(1)

    all_corrs = load_correlations(ot_dir)
    match = [
        c for c in all_corrs
        if c.get("ref") == args.event and c.get("session_id") == args.session
    ]
    if not match:
        print("Event not found in correlations.", file=sys.stderr)
        sys.exit(1)

    from opentriage.remediation.engine import run_remediation
    rems = run_remediation(match, cfg, ot_dir, ol_dir)
    for r in rems:
        print(f"Remediation: {r.get('fingerprint_slug')} → exit_code={r.get('handler_exit_code')}")


def _cmd_status(ot_dir: Path) -> None:
    from opentriage.circuit_breaker import load_state
    state = load_state(ot_dir)

    print(f"Circuit Breaker State: {state.get('circuit_breaker', 'unknown')}")
    print(f"Last Triage Run:       {_fmt_ts(state.get('last_triage_run'))}")
    print(f"Last Health Run:       {_fmt_ts(state.get('last_health_run'))}")
    print()
    print("Rolling Metrics:")
    print(f"  Remediation Success Rate: {_fmt_pct(state.get('rolling_remediation_success_rate'))}")
    print(f"  Override Rate:            {_fmt_pct(state.get('rolling_override_rate'))}")
    print(f"  Net Remediation Effect:   {_fmt_pct(state.get('net_remediation_effect'))}")
    print(f"  Total Remediations:       {state.get('total_remediations', 0)}")
    print(f"  Total Escalations:        {state.get('total_escalations', 0)}")
    print(f"  Provider Errors:          {state.get('consecutive_provider_errors', 0)}")
    print()

    # Promotion eligibility
    promo = state.get("human_approved_promotion", False)
    print(f"Human Approved Promotion: {promo}")
    if promo:
        from opentriage.circuit_breaker import evaluate_promotions
        cfg = Config.load(ot_dir / "config.toml")
        new = evaluate_promotions(state, cfg.circuit_breaker)
        if new:
            print(f"  → Would promote to: {new}")
        else:
            print("  → Metrics do not qualify for promotion yet")

    # Demotion history (last 10)
    history = state.get("demotion_history", [])
    if history:
        print(f"\nDemotion History (last {min(len(history), 10)}):")
        for entry in history[-10:]:
            ts = _fmt_ts(entry.get("ts"))
            print(f"  [{ts}] {entry.get('from')} → {entry.get('to')}: {entry.get('reason', '?')}")

    # Pending drafts
    drafts_dir = ot_dir / "drafts"
    if drafts_dir.exists():
        drafts = list(drafts_dir.glob("*.json"))
        if drafts:
            print(f"\nPending Drafts: {len(drafts)}")
            for d in drafts[:5]:
                print(f"  - {d.stem}")


def _cmd_health(cfg: Config, ot_dir: Path, args: Any) -> None:
    from opentriage.health.monitor import run_health
    result = run_health(cfg, ot_dir, days=args.days, today_only=args.today)

    print(f"Health Report: {result.get('period', '?')}")
    print(f"  Events:       {result.get('total_events', 0)}")
    print(f"  Novel:        {result.get('total_novel', 0)}")
    print(f"  Remediations: {result.get('total_remediations', 0)} (success: {result.get('total_successes', 0)})")
    print(f"  Total Cost:   ${result.get('total_cost_usd', 0):.2f}")

    # Trend detection
    daily = result.get("daily", [])
    if daily:
        from opentriage.health.trends import detect_trends
        today_metrics = daily[-1]
        alerts = detect_trends(cfg, ot_dir, today_metrics)
        if alerts:
            print(f"\nTrend Alerts ({len(alerts)}):")
            for a in alerts:
                print(f"  [{a.get('severity', '?').upper()}] {a.get('title', '?')}")
            # Escalate
            from opentriage.escalation.router import send_alert
            for a in alerts:
                send_alert(a, cfg.escalation, ot_dir)

    # Update state
    from opentriage.circuit_breaker import load_state
    from opentriage.io.writer import write_state
    state = load_state(ot_dir)
    state["last_health_run"] = time.time()
    write_state(ot_dir, state)


def _cmd_watch(cfg: Config, ot_dir: Path, ol_dir: Path, args: Any) -> None:
    from datetime import datetime, timezone

    interval = args.interval
    last_health_date = ""

    running = True
    def _signal_handler(signum: int, frame: Any) -> None:
        nonlocal running
        print("\nShutting down gracefully...")
        running = False

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    print(f"Watch mode: triage every {interval}s, health once daily. Ctrl+C to stop.")

    while running:
        # Triage cycle
        try:
            provider = _build_provider(cfg)
            from opentriage.triage.engine import run_triage
            result = run_triage(cfg, provider, ot_dir, ol_dir)
            processed = result.get("events_processed", 0)
            if processed > 0:
                print(f"[{_now_str()}] Triage: {processed} events")
        except Exception as e:
            logging.error("Triage cycle error: %s", e)

        # Daily health
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != last_health_date:
            try:
                from opentriage.health.monitor import run_health
                run_health(cfg, ot_dir, days=1, today_only=True)
                last_health_date = today
                print(f"[{_now_str()}] Health check completed")
            except Exception as e:
                logging.error("Health cycle error: %s", e)

        # Wait
        for _ in range(interval):
            if not running:
                break
            time.sleep(1)

    print("Watch mode stopped.")


def _cmd_promote(ot_dir: Path, cfg: Config) -> None:
    from opentriage.circuit_breaker import load_state, evaluate_promotions
    from opentriage.io.writer import write_state

    state = load_state(ot_dir)
    current = state.get("circuit_breaker", "suspended")
    print(f"Current state: {current}")

    state["human_approved_promotion"] = True
    new = evaluate_promotions(state, cfg.circuit_breaker)
    if new:
        print(f"Promotion approved. Next evaluation will promote to: {new}")
    else:
        print("Promotion flag set. Metrics do not yet qualify for promotion.")
        print(f"  Success rate: {_fmt_pct(state.get('rolling_remediation_success_rate'))}")
        print(f"  Recovery threshold: {cfg.circuit_breaker.get('recovery_threshold', 0.80)}")

    write_state(ot_dir, state)


def _cmd_config(config_path: Path | None, openlog_override: Path | None, key: str | None, value: str | None) -> None:
    ot_dir, _ = resolve_paths(openlog_dir=openlog_override)
    if config_path:
        ot_dir = config_path.parent
    cfg_path = ot_dir / "config.toml"

    if not cfg_path.exists():
        print("Run 'opentriage init' first.", file=sys.stderr)
        sys.exit(1)

    cfg = Config.load(cfg_path)

    if key is None:
        # Print all
        for section in ("provider", "budget", "circuit_breaker", "triage", "escalation", "remediation", "health"):
            data = getattr(cfg, section)
            print(f"[{section}]")
            for k, v in data.items():
                print(f"  {k} = {v}")
            print()
    elif value is None:
        # Print one
        try:
            print(cfg.get(key))
        except KeyError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
    else:
        # Set one
        try:
            cfg.set(key, value)
            cfg.save(cfg_path)
            print(f"Set {key} = {cfg.get(key)}")
        except KeyError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)


def _cmd_drafts(ot_dir: Path, args: Any) -> None:
    from opentriage.io.reader import read_json

    drafts_dir = ot_dir / "drafts"
    if not drafts_dir.exists():
        print("No drafts directory found.")
        return

    draft_files = sorted(drafts_dir.glob("*.json"))
    if not draft_files:
        print("No pending drafts.")
        return

    drafts = []
    for f in draft_files:
        data = read_json(f)
        if data:
            drafts.append(data)

    if args.json_output:
        print(json.dumps(drafts, indent=2, default=str))
        return

    print(f"Pending Drafts ({len(drafts)}):")
    for d in drafts:
        slug = d.get("slug", "?")
        count = d.get("recurrence_count", 1)
        created = d.get("created", "?")
        confidence = d.get("source_event", {}).get("confidence", d.get("status", "?"))
        print(f"  {slug:<40} events={count:<4} created={created}  status={confidence}")


def _cmd_approve(ot_dir: Path, ol_dir: Path, args: Any) -> None:
    from opentriage.io.reader import read_json
    from opentriage.io.writer import write_json

    slug = args.slug
    draft_path = ot_dir / "drafts" / f"{slug}.json"

    if not draft_path.exists():
        print(f"Draft not found: {slug}", file=sys.stderr)
        sys.exit(1)

    draft = read_json(draft_path)

    # Validate required fields
    missing = [f for f in ("patterns", "severity") if not draft.get(f)]
    if missing:
        print(f"Draft missing required fields: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    # Build fingerprint entry
    fingerprint = {
        "slug": draft.get("slug", slug),
        "patterns": draft.get("patterns", []),
        "severity": draft.get("severity"),
        "category": draft.get("category", "auto-approved"),
        "count": draft.get("recurrence_count", 1),
        "status": "confirmed",
        "remedy": draft.get("remedy"),
        "description": draft.get("description", ""),
    }
    if args.comment:
        fingerprint["approval_comment"] = args.comment

    # Append to fingerprints registry
    fp_path = ol_dir / "fingerprints.json"
    if fp_path.exists():
        fp_data = json.loads(fp_path.read_text())
    else:
        fp_data = []

    if isinstance(fp_data, dict) and "fingerprints" in fp_data:
        fps = fp_data["fingerprints"]
        if isinstance(fps, dict):
            # Dict-keyed format: {slug: {patterns, ...}}
            fps[fingerprint["slug"]] = {k: v for k, v in fingerprint.items() if k != "slug"}
        else:
            fps.append(fingerprint)
    elif isinstance(fp_data, list):
        fp_data.append(fingerprint)
    else:
        fp_data = [fingerprint]

    fp_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(fp_path, fp_data)

    # Move draft to approved/
    approved_dir = ot_dir / "drafts" / "approved"
    approved_dir.mkdir(parents=True, exist_ok=True)
    draft["approved_at"] = time.time()
    if args.comment:
        draft["approval_comment"] = args.comment
    (approved_dir / f"{slug}.json").write_text(json.dumps(draft, indent=2) + "\n")
    draft_path.unlink()

    print(f"Approved: {slug} → added to fingerprints registry")


def _cmd_reject(ot_dir: Path, args: Any) -> None:
    from opentriage.io.reader import read_json

    slug = args.slug
    draft_path = ot_dir / "drafts" / f"{slug}.json"

    if not draft_path.exists():
        print(f"Draft not found: {slug}", file=sys.stderr)
        sys.exit(1)

    draft = read_json(draft_path)
    draft["rejected_reason"] = args.reason
    draft["rejected_at"] = time.time()

    rejected_dir = ot_dir / "drafts" / "rejected"
    rejected_dir.mkdir(parents=True, exist_ok=True)
    (rejected_dir / f"{slug}.json").write_text(json.dumps(draft, indent=2) + "\n")
    draft_path.unlink()

    print(f"Rejected: {slug}")


def _cmd_escalations(ot_dir: Path, args: Any) -> None:
    from opentriage.io.reader import load_escalations

    records = load_escalations(ot_dir)

    if not records:
        print("No escalations found.")
        return

    last_n = records[-args.last:]

    if args.json_output:
        for r in last_n:
            print(json.dumps(r, default=str))
        return

    print(f"Recent escalations (last {len(last_n)}):")
    for r in last_n:
        ts = _fmt_ts(r.get("ts"))
        severity = r.get("severity", "?")
        etype = r.get("type", "?")
        title = r.get("title", "?")[:60]
        delivery = r.get("delivery_status", r.get("channel", "?"))
        channel = r.get("channel", "?")
        print(f"  [{ts}] {severity.upper():<8} {title}  channel={channel} status={delivery}")


def _cmd_validate(config_path: Path | None, openlog_override: Path | None) -> None:
    ot_dir, ol_dir = resolve_paths(openlog_dir=openlog_override)
    if config_path:
        ot_dir = config_path.parent

    all_ok = True

    # Check .opentriage/ exists
    if ot_dir.exists():
        print("\u2705 .opentriage/ directory exists")
    else:
        print("\u274c .opentriage/ directory missing — run 'opentriage init'")
        all_ok = False

    # Check config.toml
    cfg_path = ot_dir / "config.toml"
    if cfg_path.exists():
        try:
            Config.load(cfg_path)
            print("\u2705 config.toml is valid")
        except Exception as e:
            print(f"\u274c config.toml parse error: {e}")
            all_ok = False
    else:
        print("\u274c config.toml not found")
        all_ok = False

    # Check .openlog/
    if ol_dir.exists():
        events_dir = ol_dir / "events"
        event_count = len(list(events_dir.glob("*.jsonl"))) if events_dir.exists() else 0
        print(f"\u2705 .openlog/ exists ({event_count} event files)")
    else:
        print("\u274c .openlog/ directory missing")
        all_ok = False

    # Check provider API key
    if cfg_path.exists():
        try:
            cfg = Config.load(cfg_path)
            key_env = cfg.provider.get("api_key_env", "ANTHROPIC_API_KEY")
            if os.environ.get(key_env):
                print(f"\u2705 {key_env} is set")
            else:
                print(f"\u274c {key_env} not set in environment")
                all_ok = False
        except Exception:
            print("\u274c Could not read provider config for API key check")
            all_ok = False
    else:
        print("\u274c Skipping API key check (no config)")
        all_ok = False

    # Check state.json
    state_path = ot_dir / "state.json"
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text())
            if "circuit_breaker" in data:
                print(f"\u2705 state.json is valid (state: {data['circuit_breaker']})")
            else:
                print("\u274c state.json missing 'circuit_breaker' key")
                all_ok = False
        except json.JSONDecodeError:
            print("\u274c state.json is not valid JSON")
            all_ok = False
    else:
        print("\u274c state.json not found")
        all_ok = False

    sys.exit(0 if all_ok else 1)


def _cmd_calibrate(ot_dir: Path, ol_dir: Path, args: Any) -> None:
    from opentriage.io.reader import load_correlations, load_fingerprints

    correlations = load_correlations(ot_dir)
    fingerprints = load_fingerprints(ol_dir)
    fp_slugs = {fp.get("slug") for fp in fingerprints}

    # Find events that have both LLM classification and a matching fingerprint
    calibration_set = []
    for c in correlations:
        classification = c.get("classification")
        matched = c.get("matched_fingerprint") or c.get("fingerprint_slug")
        if classification and matched:
            calibration_set.append(c)

    # Take last N
    calibration_set = calibration_set[-args.events:]

    if not calibration_set:
        print("No events with both LLM classification and fingerprint match found.")
        print("Run more triage cycles to build calibration data.")
        return

    agree = 0
    disagree = 0
    for c in calibration_set:
        classification = c.get("classification")
        matched = c.get("matched_fingerprint") or c.get("fingerprint_slug")
        # Agreement: classified as known AND matched a confirmed fingerprint
        # OR classified as novel AND no confirmed fingerprint match
        if classification == "known" and matched in fp_slugs:
            agree += 1
        elif classification == "novel" and matched not in fp_slugs:
            agree += 1
        else:
            disagree += 1

    total = agree + disagree
    rate = agree / total if total > 0 else 0

    print(f"Calibration Report ({len(calibration_set)} events):")
    print(f"  Agreement rate: {rate:.1%}")
    print(f"  Agree: {agree}, Disagree: {disagree}")
    if rate < 0.7:
        print("  WARNING: Low agreement rate. Consider adjusting confidence thresholds.")
    elif rate > 0.9:
        print("  Good agreement. Thresholds appear well-tuned.")
    else:
        print("  Acceptable agreement. Monitor for trends.")


def _cmd_revert(ot_dir: Path, args: Any) -> None:
    from opentriage.io.reader import load_remediations, read_jsonl
    from opentriage.io.writer import write_state
    from opentriage.circuit_breaker import load_state

    rem_id = args.remediation_id
    rem_dir = ot_dir / "remediations"

    if not rem_dir.exists():
        print("No remediations directory found.", file=sys.stderr)
        sys.exit(1)

    # Find the remediation across date files
    found = False
    for jsonl_file in sorted(rem_dir.glob("*.jsonl")):
        records = read_jsonl(jsonl_file)
        updated = []
        for r in records:
            if r.get("id") == rem_id or r.get("remediation_id") == rem_id or r.get("fingerprint_slug") == rem_id:
                r["outcome"] = "reverted"
                r["reverted_at"] = time.time()
                found = True
                print(f"Reverted: {r.get('fingerprint_slug', rem_id)}")
                print(f"  Previous outcome: {r.get('outcome', 'unknown')}")
                print(f"  File: {jsonl_file.name}")
            updated.append(r)

        if found:
            # Rewrite the file with updated records
            with open(jsonl_file, "w") as f:
                for r in updated:
                    f.write(json.dumps(r, separators=(",", ":")) + "\n")
            break

    if not found:
        print(f"Remediation not found: {rem_id}", file=sys.stderr)
        sys.exit(1)

    # Update circuit breaker rolling success rate
    state = load_state(ot_dir)
    rate = state.get("rolling_remediation_success_rate")
    if rate is not None:
        # Nudge rate down slightly to reflect the revert
        total = max(state.get("total_remediations", 1), 1)
        new_rate = max(0, (rate * total - 1) / total)
        state["rolling_remediation_success_rate"] = round(new_rate, 4)
        print(f"  Success rate: {rate:.1%} → {new_rate:.1%}")
    write_state(ot_dir, state)


def _cmd_cleanup(ot_dir: Path, args: Any) -> None:
    from datetime import datetime, timezone

    days = args.older_than
    dry_run = args.dry_run
    cutoff = time.time() - (days * 86400)

    dirs_to_clean = ["correlations", "remediations", "metrics"]
    total_files = 0
    total_bytes = 0

    for subdir in dirs_to_clean:
        target = ot_dir / subdir
        if not target.exists():
            continue

        for f in sorted(target.glob("*.*")):
            # Try to parse date from filename (YYYY-MM-DD.jsonl or .json)
            stem = f.stem
            try:
                file_date = datetime.strptime(stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                file_ts = file_date.timestamp()
            except ValueError:
                # Fall back to file modification time
                file_ts = f.stat().st_mtime

            if file_ts < cutoff:
                size = f.stat().st_size
                if dry_run:
                    print(f"  Would remove: {subdir}/{f.name} ({size} bytes)")
                else:
                    f.unlink()
                total_files += 1
                total_bytes += size

    action = "Would remove" if dry_run else "Removed"
    print(f"{action}: {total_files} files ({total_bytes:,} bytes)")
    if dry_run and total_files > 0:
        print("Run without --dry-run to delete.")


# --- Helpers ---

def _build_provider(cfg: Config) -> Any:
    """Build an LLM provider from config. Returns None if unavailable."""
    backend = cfg.provider.get("backend", "anthropic")
    try:
        if backend == "anthropic":
            from opentriage.provider.anthropic import AnthropicProvider
            return AnthropicProvider(**cfg.provider)
        elif backend == "openai":
            from opentriage.provider.openai import OpenAIProvider
            return OpenAIProvider(**cfg.provider)
        elif backend == "ollama":
            from opentriage.provider.ollama import OllamaProvider
            return OllamaProvider(**cfg.provider)
        else:
            logging.warning("Unknown provider backend: %s", backend)
            return None
    except Exception as e:
        logging.warning("Provider unavailable: %s", e)
        return None


def _acquire_lock(ot_dir: Path) -> Path | None:
    """Acquire triage lock file. Returns lock path or None."""
    lock_path = ot_dir / ".triage.lock"
    if lock_path.exists():
        try:
            data = json.loads(lock_path.read_text())
            lock_ts = data.get("ts", 0)
            pid = data.get("pid", 0)
            # Stale after 1 hour
            if time.time() - lock_ts > 3600:
                logging.warning("Stale lock detected (PID %d), overriding", pid)
            else:
                print(
                    f"Another triage cycle is running (PID {pid}). "
                    "Use --force to override stale lock.",
                    file=sys.stderr,
                )
                return None
        except (json.JSONDecodeError, OSError):
            pass

    lock_path.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}))
    return lock_path


def _release_lock(ot_dir: Path) -> None:
    lock_path = ot_dir / ".triage.lock"
    if lock_path.exists():
        lock_path.unlink(missing_ok=True)


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "never"
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_pct(val: float | None) -> str:
    if val is None:
        return "N/A (insufficient data)"
    return f"{val:.1%}"


def _now_str() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%H:%M:%S")
