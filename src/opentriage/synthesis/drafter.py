"""Novel pattern synthesis — draft fingerprint generation (F-OT06)."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opentriage.io.reader import load_fingerprints, load_session_events, read_json
from opentriage.io.writer import write_draft
from opentriage.provider.protocol import LLMProvider, ProviderError

log = logging.getLogger(__name__)


def run_synthesis(
    novel_correlations: list[dict[str, Any]],
    provider: LLMProvider | None,
    opentriage_dir: Path,
    openlog_dir: Path,
) -> list[dict[str, Any]]:
    """Draft fingerprints for confirmed novel patterns. Returns draft records."""
    if not novel_correlations:
        return []

    fingerprints = load_fingerprints(openlog_dir)
    drafts: list[dict[str, Any]] = []

    for corr in novel_correlations:
        if corr.get("classification") != "novel":
            continue
        if corr.get("confidence") not in ("high", "medium"):
            continue

        draft = _synthesize_one(corr, fingerprints, provider, opentriage_dir, openlog_dir)
        if draft:
            drafts.append(draft)

    # Novel burst detection
    if len(drafts) >= 5:
        log.warning("Novel burst: %d novel patterns in one cycle", len(drafts))

    return drafts


def _synthesize_one(
    corr: dict[str, Any],
    fingerprints: list[dict[str, Any]],
    provider: LLMProvider | None,
    opentriage_dir: Path,
    openlog_dir: Path,
) -> dict[str, Any] | None:
    """Synthesize a single draft fingerprint."""
    session_events = load_session_events(openlog_dir, corr.get("session_id", ""))

    if provider is None:
        # Minimal draft without LLM
        return _save_minimal_draft(corr, opentriage_dir)

    # Build synthesis prompt
    fp_summary = "\n".join(
        f"  {fp.get('slug', '?')}: {(fp.get('patterns') or [''])[0][:60]}"
        for fp in fingerprints if fp.get("status") == "confirmed"
    )

    session_ctx = "\n".join(
        f"  [{e.get('kind', '?')}] {e.get('ref', '?')}: {str(e.get('f_raw', e.get('message', '')))[:100]}"
        for e in session_events[:20]
    )

    prompt = f"""You are a failure pattern analyst. A novel failure has been confirmed.
Draft a new fingerprint entry for the failure registry.

ERROR EVENT:
{json.dumps(corr, indent=2, default=str)}

SESSION CONTEXT (all events from this session):
{session_ctx or '(no session events)'}

CLASSIFICATION CHAIN:
Classification: {corr.get('classification')}
Confidence: {corr.get('confidence')}
Reasoning: {corr.get('reasoning', 'N/A')}

EXISTING PATTERNS (for deduplication):
{fp_summary or '(none)'}

Respond with JSON:
{{
  "slug": "lowercase-hyphenated-max-40-chars",
  "description": "1-2 sentence description of the failure class",
  "patterns": ["the f_raw from this event", "1-2 alternative phrasings"],
  "severity": "fatal" | "recoverable" | null,
  "remedy": "suggested fix in 1-3 sentences, or null if unknown",
  "root_cause_hypothesis": "HYPOTHESIS — 1-3 sentences, not verified",
  "dedup_check": "This is NOT a variant of {{closest_slug}} because..."
}}"""

    messages = [{"role": "user", "content": prompt}]

    try:
        response = provider.complete(messages, tier="expensive")
        draft_data = _parse_draft(response)
        if not draft_data:
            # Retry once
            messages.append({"role": "user", "content": "Respond with valid JSON only."})
            response = provider.complete(messages, tier="expensive")
            draft_data = _parse_draft(response)

        if not draft_data:
            return _save_minimal_draft(corr, opentriage_dir)

        return _save_draft(draft_data, corr, fingerprints, opentriage_dir)

    except ProviderError as e:
        log.warning("Synthesis provider error: %s", e)
        return _save_minimal_draft(corr, opentriage_dir)


def _parse_draft(response: str) -> dict[str, Any] | None:
    """Parse draft JSON from LLM response."""
    response = response.strip()
    start = response.find("{")
    end = response.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            data = json.loads(response[start:end])
            if "slug" in data and "patterns" in data:
                return data
        except json.JSONDecodeError:
            pass
    return None


def _save_draft(
    draft_data: dict[str, Any],
    corr: dict[str, Any],
    fingerprints: list[dict[str, Any]],
    opentriage_dir: Path,
) -> dict[str, Any]:
    """Save a full draft fingerprint."""
    slug = draft_data.get("slug", "unknown-pattern")[:40]

    # Check slug collision with existing fingerprints
    existing_slugs = {fp.get("slug") for fp in fingerprints}
    if slug in existing_slugs:
        slug = f"{slug}-draft"

    # Check if draft already exists (recurrence)
    drafts_dir = opentriage_dir / "drafts"
    existing_draft_path = drafts_dir / f"{slug}.json"
    if existing_draft_path.exists():
        existing = read_json(existing_draft_path)
        existing["recurrence_count"] = existing.get("recurrence_count", 1) + 1
        existing["last_seen"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        write_draft(opentriage_dir, slug, existing)
        return existing

    # Determine status
    status = "proposed"
    dedup = draft_data.get("dedup_check", "")
    if dedup and "IS a variant" in dedup:
        status = "likely_variant"

    draft = {
        "slug": slug,
        "description": draft_data.get("description", ""),
        "patterns": draft_data.get("patterns", [corr.get("f_raw", "")]),
        "severity": draft_data.get("severity"),
        "remedy": draft_data.get("remedy"),
        "root_cause_hypothesis": draft_data.get("root_cause_hypothesis", "HYPOTHESIS — unknown"),
        "dedup_check": dedup,
        "source_event": {
            "session_id": corr.get("session_id"),
            "ref": corr.get("ref"),
            "ts": corr.get("ts"),
        },
        "status": status,
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "recurrence_count": 1,
    }

    write_draft(opentriage_dir, slug, draft)
    return draft


def _save_minimal_draft(corr: dict[str, Any], opentriage_dir: Path) -> dict[str, Any]:
    """Save a minimal draft when LLM is unavailable."""
    f_raw = corr.get("f_raw", "unknown")
    slug = f_raw[:40].lower().replace(" ", "-").replace("_", "-")
    slug = "".join(c for c in slug if c.isalnum() or c == "-")[:40]
    if not slug:
        slug = "unknown-pattern"

    draft = {
        "slug": slug,
        "description": "",
        "patterns": [f_raw],
        "severity": None,
        "remedy": None,
        "root_cause_hypothesis": "HYPOTHESIS — not analyzed (provider unavailable)",
        "dedup_check": "",
        "source_event": {
            "session_id": corr.get("session_id"),
            "ref": corr.get("ref"),
            "ts": corr.get("ts"),
        },
        "status": "incomplete",
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "recurrence_count": 1,
    }

    write_draft(opentriage_dir, slug, draft)
    return draft
