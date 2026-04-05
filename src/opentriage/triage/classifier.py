"""LLM-powered classification (slow path + confirmation path)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from opentriage.provider.protocol import LLMProvider, ProviderError

log = logging.getLogger(__name__)


def build_triage_prompt(
    event: dict[str, Any],
    fingerprints: list[dict[str, Any]],
    candidate_slug: str | None = None,
    candidate_similarity: float = 0.0,
) -> list[dict]:
    """Build the cheap-tier triage classification prompt."""
    fp_summary = ""
    for fp in fingerprints:
        if fp.get("status") != "confirmed":
            continue
        slug = fp.get("slug", "")
        first_pattern = (fp.get("patterns") or [""])[0][:80]
        severity = fp.get("severity") or "null"
        remedy = (fp.get("remedy") or "")[:50]
        fp_summary += f"  - {slug}: \"{first_pattern}\" severity={severity} remedy=\"{remedy}\"\n"

    candidate_hint = ""
    if candidate_slug:
        candidate_hint = f"\nClosest match: {candidate_slug} (similarity: {candidate_similarity:.2f})"

    user_content = f"""You are a failure classifier. Classify this agent error event.

EVENT:
f_raw: {event.get('f_raw', '')}
stderr: {str(event.get('stderr', ''))[:500]}
exit_code: {event.get('exit_code', 'N/A')}
ref: {event.get('ref', 'N/A')}

KNOWN FAILURE PATTERNS:
{fp_summary or '  (none)'}
{candidate_hint}

Respond with JSON only:
{{"classification":"known-pattern"|"novel"|"transient","matched_fingerprint":"slug or null","confidence":"high"|"medium"|"low","reasoning":"1-2 sentences"}}"""

    return [{"role": "user", "content": user_content}]


def build_confirmation_prompt(
    event: dict[str, Any],
    cheap_result: dict[str, Any],
    fingerprints: list[dict[str, Any]],
    session_events: list[dict[str, Any]],
) -> list[dict]:
    """Build the standard-tier confirmation prompt."""
    # Full fingerprint entry for matched pattern
    matched_fp = ""
    if cheap_result.get("matched_fingerprint"):
        for fp in fingerprints:
            if fp.get("slug") == cheap_result["matched_fingerprint"]:
                matched_fp = json.dumps(fp, indent=2)
                break

    # For novel: 3 closest fingerprints
    closest_fps = ""
    if cheap_result.get("classification") == "novel":
        from opentriage.triage.matcher import trigram_similarity
        scored = []
        for fp in fingerprints:
            if fp.get("status") != "confirmed":
                continue
            best_sim = 0.0
            for p in fp.get("patterns", []):
                sim = trigram_similarity(event.get("f_raw", ""), p)
                best_sim = max(best_sim, sim)
            scored.append((fp.get("slug", ""), best_sim))
        scored.sort(key=lambda x: x[1], reverse=True)
        closest_fps = "\n".join(f"  - {s}: similarity={sim:.2f}" for s, sim in scored[:3])

    session_ctx = ""
    for se in session_events[:20]:
        session_ctx += f"  [{se.get('kind', '?')}] {se.get('ref', '?')}: {str(se.get('f_raw', se.get('message', '')))[:100]}\n"

    user_content = f"""You are a senior failure analyst. Confirm or override this classification.

EVENT (full):
{json.dumps(event, indent=2, default=str)}

CHEAP-TIER CLASSIFICATION:
classification: {cheap_result.get('classification')}
matched_fingerprint: {cheap_result.get('matched_fingerprint')}
confidence: {cheap_result.get('confidence')}
reasoning: {cheap_result.get('reasoning')}

MATCHED FINGERPRINT ENTRY:
{matched_fp or '(none)'}

CLOSEST FINGERPRINTS (for novel):
{closest_fps or '(N/A)'}

SESSION CONTEXT:
{session_ctx or '(no session events)'}

Respond with JSON only:
{{"classification":"known-pattern"|"novel"|"transient","matched_fingerprint":"slug or null","confidence":"high"|"medium"|"low","reasoning":"1-2 sentences","overrides_cheap":true|false}}"""

    return [{"role": "user", "content": user_content}]


def classify_with_llm(
    provider: LLMProvider,
    messages: list[dict],
    tier: str = "cheap",
) -> dict[str, Any]:
    """Send classification prompt and parse JSON response.

    Retries once on parse failure with an instruction to respond with valid JSON.
    """
    for attempt in range(2):
        try:
            response = provider.complete(messages, tier=tier)
            result = _parse_classification(response)
            if result:
                return result
            # Retry with JSON instruction
            if attempt == 0:
                messages = messages + [
                    {"role": "user", "content": "Respond with valid JSON only."}
                ]
        except ProviderError as e:
            log.warning("Provider error on %s tier (attempt %d): %s", tier, attempt + 1, e)
            if attempt == 0:
                time.sleep(2)

    return {
        "classification": "deferred",
        "matched_fingerprint": None,
        "confidence": "low",
        "reasoning": "Failed to get valid classification from provider",
    }


def _parse_classification(response: str) -> dict[str, Any] | None:
    """Parse a classification JSON from LLM response text."""
    response = response.strip()
    # Try to find JSON in the response
    start = response.find("{")
    end = response.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            data = json.loads(response[start:end])
            required = {"classification", "matched_fingerprint", "confidence", "reasoning"}
            if required.issubset(data.keys()):
                if data["classification"] in ("known-pattern", "novel", "transient", "deferred"):
                    return data
        except json.JSONDecodeError:
            pass
    return None
