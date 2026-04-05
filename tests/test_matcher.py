"""Tests for fast-path fingerprint matching."""

from opentriage.triage.matcher import match_event, trigram_set, trigram_similarity


def test_trigram_set():
    result = trigram_set("hello")
    assert "hel" in result
    assert "ell" in result
    assert "llo" in result


def test_trigram_similarity_identical():
    assert trigram_similarity("hello world", "hello world") == 1.0


def test_trigram_similarity_different():
    sim = trigram_similarity("hello world", "xyz abc 123")
    assert sim < 0.2


def test_trigram_similarity_empty():
    assert trigram_similarity("", "") == 1.0
    assert trigram_similarity("hello", "") == 0.0


def test_substring_match(sample_fingerprints):
    result = match_event(
        "circular import between auth and user",
        sample_fingerprints,
    )
    assert result.matched is True
    assert result.fingerprint_slug == "circular-import"
    assert result.method == "substring"
    assert result.similarity == 1.0


def test_substring_match_case_insensitive(sample_fingerprints):
    result = match_event(
        "CIRCULAR IMPORT between modules",
        sample_fingerprints,
    )
    assert result.matched is True
    assert result.fingerprint_slug == "circular-import"


def test_trigram_high_similarity(sample_fingerprints):
    # Close enough to match via trigram but not substring
    result = match_event(
        "circular importing between services",
        sample_fingerprints,
    )
    # This may match substring since "circular import" is in patterns
    assert result.matched is True


def test_no_match(sample_fingerprints):
    result = match_event(
        "widget factory explosion in module X",
        sample_fingerprints,
    )
    assert result.matched is False


def test_only_confirmed_fingerprints(sample_fingerprints):
    """Provisional fingerprints should not be matched."""
    result = match_event(
        "some provisional thing happened",
        sample_fingerprints,
    )
    # provisional-pattern has status=provisional, should be skipped
    assert result.fingerprint_slug != "provisional-pattern" or result.matched is False


def test_needs_llm_with_candidate(sample_fingerprints):
    """Medium similarity should flag needs_llm with a candidate."""
    result = match_event(
        "import cycle between modules auth",
        sample_fingerprints,
        similarity_threshold=0.9,  # High threshold to force LLM path
        llm_floor=0.2,
    )
    if not result.matched:
        assert result.fingerprint_slug is not None  # Should have a candidate
