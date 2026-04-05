"""Fast-path fingerprint matching — substring + trigram similarity."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MatchResult:
    matched: bool
    fingerprint_slug: str | None
    similarity: float
    method: str  # "substring", "trigram", "none"


def trigram_set(s: str) -> set[str]:
    """Generate character trigrams from a lowercased string."""
    s = s.lower().strip()
    if len(s) < 3:
        return {s} if s else set()
    return {s[i:i + 3] for i in range(len(s) - 2)}


def trigram_similarity(a: str, b: str) -> float:
    """Compute Jaccard similarity of trigram sets."""
    sa = trigram_set(a)
    sb = trigram_set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def match_event(
    f_raw: str,
    fingerprints: list[dict],
    similarity_threshold: float = 0.7,
    llm_floor: float = 0.4,
) -> MatchResult:
    """Match an event's f_raw against the fingerprint registry.

    Only considers fingerprints with status='confirmed'.

    Returns:
        MatchResult with the best match info.
    """
    f_raw_lower = f_raw.lower()
    best_slug: str | None = None
    best_sim: float = 0.0

    for fp in fingerprints:
        if fp.get("status") != "confirmed":
            continue
        slug = fp.get("slug", "")
        patterns = fp.get("patterns", [])

        # Substring match (case-insensitive)
        for pattern in patterns:
            if pattern.lower() in f_raw_lower:
                return MatchResult(
                    matched=True,
                    fingerprint_slug=slug,
                    similarity=1.0,
                    method="substring",
                )

        # Trigram similarity
        for pattern in patterns:
            sim = trigram_similarity(f_raw, pattern)
            if sim > best_sim:
                best_sim = sim
                best_slug = slug

    if best_sim >= similarity_threshold:
        return MatchResult(
            matched=True,
            fingerprint_slug=best_slug,
            similarity=best_sim,
            method="trigram",
        )

    if best_sim >= llm_floor:
        return MatchResult(
            matched=False,
            fingerprint_slug=best_slug,
            similarity=best_sim,
            method="none",
        )

    return MatchResult(
        matched=False,
        fingerprint_slug=None,
        similarity=best_sim,
        method="none",
    )
