"""Tests for fix agent spawner (F-AR04)."""

import json
from unittest.mock import MagicMock, patch

from opentriage.remediation.agent_handler import (
    RemediationResult,
    build_fix_prompt,
    verify_fix,
    spawn_fix_agent,
)
from opentriage.remediation.evidence import EvidenceBundle


def _make_evidence(**kwargs) -> EvidenceBundle:
    """Create a minimal evidence bundle for testing."""
    defaults = dict(
        attempt_id="rem-test-001",
        error_event={"f_raw": "circular import", "stderr": "ImportError", "ref": "t1"},
        screenshot_path=None,
        screenshot_note=None,
        fingerprint={"slug": "circular-import", "severity": "recoverable"},
        session_events=[],
        recent_correlations=[],
        git_context=None,
        relevant_files=["src/auth.py"],
        remedy={
            "strategy": "code-fix",
            "description": "Split shared types",
            "relevant_files": ["src/auth.py"],
            "test_command": "pytest tests/",
            "fix_prompt": "Fix the circular import in auth module",
            "max_cost_usd": 2.0,
            "requires_screenshot": False,
        },
        project_dir="/tmp/fake-project",
    )
    defaults.update(kwargs)
    return EvidenceBundle(**defaults)


def test_build_fix_prompt_basic():
    evidence = _make_evidence()
    prompt = build_fix_prompt(evidence)

    assert "circular import" in prompt
    assert "circular-import" in prompt
    assert "code-fix" in prompt
    assert "src/auth.py" in prompt
    assert "Fix the circular import" in prompt
    assert "pytest tests/" in prompt
    assert "Do NOT modify test expectations" in prompt
    assert "One bug, one fix" in prompt


def test_build_fix_prompt_with_screenshot():
    evidence = _make_evidence(screenshot_path="/tmp/screen.png")
    prompt = build_fix_prompt(evidence)
    assert "/tmp/screen.png" in prompt
    assert "Screenshot" in prompt


def test_build_fix_prompt_no_remedy():
    evidence = _make_evidence(remedy=None)
    prompt = build_fix_prompt(evidence)
    assert "Unknown error" in prompt


def test_build_fix_prompt_with_git_context():
    evidence = _make_evidence(git_context="abc1234 fix something\ndef5678 add feature")
    prompt = build_fix_prompt(evidence)
    assert "abc1234" in prompt
    assert "Git Context" in prompt


def test_remediation_result_dataclass():
    result = RemediationResult(
        attempt_id="rem-001",
        status="fixed",
        exit_code=0,
        files_changed=["src/auth.py"],
        lines_changed=10,
        commit_sha="abc123",
    )
    d = result.to_dict()
    assert d["status"] == "fixed"
    assert d["commit_sha"] == "abc123"


def test_verify_fix_no_changes():
    evidence = _make_evidence()
    with patch("opentriage.remediation.agent_handler._get_git_diff") as mock_diff:
        mock_diff.return_value = ("", [], 0)
        failures = verify_fix(evidence, "/tmp/fake")
    assert any("No files changed" in f for f in failures)


def test_verify_fix_no_overlap():
    evidence = _make_evidence(relevant_files=["src/auth.py"])
    with patch("opentriage.remediation.agent_handler._get_git_diff") as mock_diff:
        mock_diff.return_value = ("diff", ["src/unrelated.py"], 10)
        with patch("opentriage.remediation.agent_handler._check_test_modifications", return_value=[]):
            failures = verify_fix(evidence, "/tmp/fake")
    assert any("confabulation" in f.lower() for f in failures)


def test_verify_fix_too_many_files():
    evidence = _make_evidence(relevant_files=[])
    files = [f"src/file{i}.py" for i in range(8)]
    with patch("opentriage.remediation.agent_handler._get_git_diff") as mock_diff:
        mock_diff.return_value = ("diff", files, 50)
        with patch("opentriage.remediation.agent_handler._check_test_modifications", return_value=[]):
            failures = verify_fix(evidence, "/tmp/fake")
    assert any("Too many files" in f for f in failures)


def test_verify_fix_too_many_lines():
    evidence = _make_evidence(relevant_files=[])
    with patch("opentriage.remediation.agent_handler._get_git_diff") as mock_diff:
        mock_diff.return_value = ("diff", ["src/auth.py"], 300)
        with patch("opentriage.remediation.agent_handler._check_test_modifications", return_value=[]):
            failures = verify_fix(evidence, "/tmp/fake")
    assert any("Too many lines" in f for f in failures)


def test_verify_fix_all_pass():
    evidence = _make_evidence(relevant_files=["src/auth.py"])
    with patch("opentriage.remediation.agent_handler._get_git_diff") as mock_diff:
        mock_diff.return_value = ("diff", ["src/auth.py"], 15)
        with patch("opentriage.remediation.agent_handler._check_test_modifications", return_value=[]):
            failures = verify_fix(evidence, "/tmp/fake")
    assert failures == []


def test_spawn_fix_agent_no_project_dir():
    evidence = _make_evidence(project_dir=None)
    result = spawn_fix_agent(evidence, {})
    assert result.status == "failed"
    assert "No project directory" in result.output


def test_spawn_fix_agent_claude_unavailable(tmp_path):
    evidence = _make_evidence(project_dir=str(tmp_path))
    with patch("subprocess.run", side_effect=FileNotFoundError("claude not found")):
        result = spawn_fix_agent(evidence, {}, project_dir=tmp_path)
    assert result.status == "agent_unavailable"


def test_spawn_fix_agent_timeout(tmp_path):
    import subprocess as sp

    evidence = _make_evidence(project_dir=str(tmp_path))

    call_count = [0]
    def mock_run(*args, **kwargs):
        call_count[0] += 1
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and cmd[0] == "claude":
            if "--version" in cmd:
                return MagicMock(returncode=0, stdout="1.0", stderr="")
            raise sp.TimeoutExpired(cmd, 300)
        # git status --porcelain
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=mock_run):
        result = spawn_fix_agent(evidence, {"timeout_seconds": 300}, project_dir=tmp_path)
    assert result.status == "timeout"


def test_spawn_fix_agent_writes_prompt(tmp_path):
    """Verify prompt.md is written before agent spawn."""
    evidence = _make_evidence(project_dir=str(tmp_path))

    call_count = [0]
    def mock_run(*args, **kwargs):
        call_count[0] += 1
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and cmd[0] == "claude":
            if "--version" in cmd:
                return MagicMock(returncode=0, stdout="1.0", stderr="")
            # Check prompt was written
            prompt_path = tmp_path / ".opentriage" / "remediations" / evidence.attempt_id / "prompt.md"
            assert prompt_path.exists()
            return MagicMock(returncode=0, stdout="Fixed", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=mock_run):
        with patch("opentriage.remediation.agent_handler._get_git_diff", return_value=("", [], 0)):
            result = spawn_fix_agent(evidence, {}, project_dir=tmp_path)
    # Agent ran but no diff → no changes detected
    assert result.status in ("failed", "suspicious")
