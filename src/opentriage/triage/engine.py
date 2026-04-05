"""Triage engine — main classification pipeline (F-OT02)."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from opentriage.circuit_breaker import can, load_state
from opentriage.config import Config
from opentriage.io.reader import (
    load_correlations,
    load_fingerprints,
    load_session_events,
    scan_events,
)
from opentriage.io.writer import write_correlation
from opentriage.provider.protocol import LLMProvider
from opentriage.triage.classifier import (
    build_confirmation_prompt,
    build_triage_prompt,
    classify_with_llm,
)
from opentriage.triage.matcher import match_event, trigram_similarity

log = logging.getLogger(__name__)


def run_triage(
    config: Config,
    provider: LLMProvider | None,
    opentriage_dir: Path,
    openlog_dir: Path,
    window_hours: float | None = None,
    scan_all: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute one triage cycle. Returns summary dict."""
    state = load_state(opentriage_dir)

    # Check circuit breaker
    cb_state = state.get("circuit_breaker", "suspended")
    if cb_state == "suspended":
        log.info("Triage skipped: circuit breaker suspended")
        return {"status": "skipped", "reason": "circuit_breaker_suspended", "events_processed": 0}

    # Determine time window
    now = datetime.now(timezone.utc)
    if scan_all:
        window_start = None
    else:
        hours = window_hours or config.triage.get("scan_window_hours", 2)
        window_start = now - timedelta(hours=hours)

    # Scan events
    events_dir = openlog_dir / "events"
    if not events_dir.exists():
        log.info("No OpenLog events found. Is openlog-agent installed?")
        return {"status": "ok", "reason": "no_events_dir", "events_processed": 0}

    events = scan_events(openlog_dir, window_start=window_start, window_end=now)
    if not events:
        return {"status": "ok", "events_processed": 0}

    # Load fingerprints and existing correlations
    fingerprints = load_fingerprints(openlog_dir)
    existing_corrs = load_correlations(opentriage_dir)
    corr_keys = {(c.get("ts"), c.get("ref"), c.get("session_id")) for c in existing_corrs}

    # Filter uncorrelated
    uncorrelated = [
        e for e in events
        if (e.get("ts"), e.get("ref"), e.get("session_id")) not in corr_keys
    ]

    # Batch limit
    max_per_cycle = config.triage.get("max_events_per_cycle", 50)
    backlog = 0
    if len(uncorrelated) > max_per_cycle:
        backlog = len(uncorrelated) - max_per_cycle
        uncorrelated.sort(key=lambda e: e.get("ts", 0), reverse=True)
        uncorrelated = uncorrelated[:max_per_cycle]
        log.warning("Triage backlog: %d events deferred to next cycle", backlog)

    # Classification results
    results: list[dict[str, Any]] = []
    needs_llm: list[tuple[dict[str, Any], str | None, float]] = []
    stats = {
        "fast_path": 0, "slow_path": 0, "confirmed": 0, "novel": 0,
        "transient": 0, "deferred": 0, "overrides": 0, "total": len(uncorrelated),
    }

    sim_threshold = config.triage.get("fast_path_similarity_threshold", 0.7)
    llm_floor = config.triage.get("needs_llm_similarity_floor", 0.4)

    # Step 1: Fast path
    for event in uncorrelated:
        result = match_event(event["f_raw"], fingerprints, sim_threshold, llm_floor)
        if result.matched:
            corr = _make_correlation(event, result.fingerprint_slug, "high", "fast_path", result.method)
            if not dry_run:
                write_correlation(opentriage_dir, corr)
            results.append(corr)
            stats["fast_path"] += 1
        else:
            needs_llm.append((event, result.fingerprint_slug, result.similarity))

    # Step 2: Slow path (cheap-tier LLM)
    if not can(state, "classify") or provider is None:
        # Can't classify — defer remaining
        for event, _, _ in needs_llm:
            corr = _make_correlation(event, None, "low", "deferred", "no_provider")
            if not dry_run:
                write_correlation(opentriage_dir, corr)
            results.append(corr)
            stats["deferred"] += 1
    else:
        needs_confirmation: list[tuple[dict[str, Any], dict[str, Any]]] = []

        for event, candidate_slug, candidate_sim in needs_llm:
            messages = build_triage_prompt(event, fingerprints, candidate_slug, candidate_sim)
            classification = classify_with_llm(provider, messages, tier="cheap")

            if classification["classification"] == "deferred":
                # Route to standard-tier
                needs_confirmation.append((event, classification))
                stats["deferred"] += 1
                continue

            conf = classification.get("confidence", "low")
            cls = classification["classification"]

            if cls == "known-pattern" and conf == "high":
                corr = _make_correlation(
                    event, classification.get("matched_fingerprint"),
                    conf, "slow_path", "llm_cheap",
                    reasoning=classification.get("reasoning"),
                )
                if not dry_run:
                    write_correlation(opentriage_dir, corr)
                results.append(corr)
                stats["slow_path"] += 1
            elif cls == "transient":
                corr = _make_correlation(event, None, conf, "slow_path", "llm_cheap", classification="transient",
                                         reasoning=classification.get("reasoning"))
                if not dry_run:
                    write_correlation(opentriage_dir, corr)
                results.append(corr)
                stats["transient"] += 1
            else:
                # Medium confidence or novel → confirmation
                needs_confirmation.append((event, classification))

        # Step 3: Confirmation path (standard-tier LLM)
        for event, cheap_result in needs_confirmation:
            session_events = load_session_events(openlog_dir, event.get("session_id", ""))
            messages = build_confirmation_prompt(event, cheap_result, fingerprints, session_events)
            confirmed = classify_with_llm(provider, messages, tier="standard")

            overridden = confirmed.get("overrides_cheap", False)
            if overridden:
                stats["overrides"] += 1

            cls = confirmed.get("classification", cheap_result.get("classification", "novel"))
            fp_slug = confirmed.get("matched_fingerprint", cheap_result.get("matched_fingerprint"))
            conf = confirmed.get("confidence", "medium")

            corr = _make_correlation(
                event, fp_slug, conf, "confirmation_path", "llm_standard",
                classification=cls,
                reasoning=confirmed.get("reasoning"),
                overridden_by="standard" if overridden else None,
            )
            if not dry_run:
                write_correlation(opentriage_dir, corr)
            results.append(corr)

            if cls == "novel":
                stats["novel"] += 1
            else:
                stats["confirmed"] += 1

    # Step 4: Transient recurrence detection
    if not dry_run:
        _detect_transient_recurrence(config, opentriage_dir, openlog_dir, results)

    # Update state
    if not dry_run:
        state["last_triage_run"] = time.time()
        from opentriage.io.writer import write_state
        write_state(opentriage_dir, state)

    return {
        "status": "ok",
        "events_processed": len(uncorrelated),
        "backlog": backlog,
        "stats": stats,
        "correlations": results,
    }


def _make_correlation(
    event: dict[str, Any],
    fingerprint_slug: str | None,
    confidence: str,
    tier: str,
    method: str,
    classification: str | None = None,
    reasoning: str | None = None,
    overridden_by: str | None = None,
) -> dict[str, Any]:
    """Create a correlation record."""
    cls = classification or ("known-pattern" if fingerprint_slug else "novel")
    corr: dict[str, Any] = {
        "ts": event.get("ts", time.time()),
        "ref": event.get("ref"),
        "session_id": event.get("session_id"),
        "f_raw": event.get("f_raw", ""),
        "classification": cls,
        "matched_fingerprint": fingerprint_slug,
        "confidence": confidence,
        "tier": tier,
        "method": method,
    }
    if reasoning:
        corr["reasoning"] = reasoning
    if overridden_by:
        corr["overridden_by"] = overridden_by
    return corr


def _detect_transient_recurrence(
    config: Config,
    opentriage_dir: Path,
    openlog_dir: Path,
    new_correlations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Check for transient patterns that recur enough to be reclassified as novel."""
    threshold = config.triage.get("transient_recurrence_threshold", 3)
    window_hours = config.triage.get("transient_recurrence_window_hours", 24)
    cutoff = time.time() - (window_hours * 3600)

    all_corrs = load_correlations(opentriage_dir)
    transients = [
        c for c in all_corrs
        if c.get("classification") == "transient" and c.get("ts", 0) >= cutoff
    ]

    if len(transients) < threshold:
        return []

    # Check pairwise similarity
    reclassified: list[dict[str, Any]] = []
    clusters: list[list[dict[str, Any]]] = []

    for t in transients:
        placed = False
        for cluster in clusters:
            ref_raw = cluster[0].get("f_raw", "")
            sim = trigram_similarity(t.get("f_raw", ""), ref_raw)
            if sim >= 0.6:
                cluster.append(t)
                placed = True
                break
        if not placed:
            clusters.append([t])

    for cluster in clusters:
        if len(cluster) >= threshold:
            most_recent = max(cluster, key=lambda c: c.get("ts", 0))
            novel_corr = _make_correlation(
                most_recent, None, "medium", "recurrence_detection", "transient_reclassify",
                classification="novel",
                reasoning=f"Reclassified: {len(cluster)} transient events with similarity >= 0.6 in {window_hours}h",
            )
            write_correlation(opentriage_dir, novel_corr)
            reclassified.append(novel_corr)

    return reclassified
