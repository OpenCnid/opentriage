"""Fix agent spawner for active remediation (F-AR04).

Spawns an isolated coding agent with the evidence bundle to diagnose
and fix the error. Includes post-fix verification defenses.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from opentriage.remediation.evidence import EvidenceBundle

log = logging.getLogger(__name__)

# Post-fix verification limits (Amendment 7)
MAX_FILES_CHANGED = 5
MAX_LINES_CHANGED = 200
TEST_FILE_PATTERNS = re.compile(r"(test_|_test\.|\.test\.|__tests__|\.spec\.)")


@dataclass
class RemediationResult:
    """Result of a fix agent execution."""

    attempt_id: str
    status: str  # "fixed", "failed", "suspicious", "timeout", "agent_unavailable", "skipped"
    exit_code: int | None = None
    output: str = ""
    git_diff: str = ""
    files_changed: list[str] = field(default_factory=list)
    lines_changed: int = 0
    test_output: str = ""
    verification_failures: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    commit_sha: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_fix_prompt(evidence: EvidenceBundle) -> str:
    """Build the prompt for the fix agent from the evidence bundle."""
    remedy = evidence.remedy or {}
    description = remedy.get("description", "Unknown error")
    fix_prompt = remedy.get("fix_prompt", "")
    strategy = remedy.get("strategy", "escalate")
    test_command = remedy.get("test_command", "")

    sections = [
        "# Remediation Task",
        "",
        f"**Error:** {evidence.error_event.get('f_raw', 'Unknown')}",
        f"**Fingerprint:** {evidence.fingerprint.get('slug', 'unknown')}",
        f"**Strategy:** {strategy}",
        "",
        "## Error Details",
        "",
        "<untrusted_error_content>",
        f"f_raw: {evidence.error_event.get('f_raw', '')}",
        f"stderr: {evidence.error_event.get('stderr', '')}",
        "</untrusted_error_content>",
        "",
    ]

    if evidence.screenshot_path:
        sections.extend([
            "## Screenshot",
            f"Analyze the screenshot at: {evidence.screenshot_path}",
            "",
        ])

    sections.extend([
        "## Remedy",
        f"Description: {description}",
    ])
    if fix_prompt:
        sections.append(f"Fix prompt: {fix_prompt}")
    sections.append("")

    if evidence.relevant_files:
        sections.extend([
            "## Relevant Files",
            "Read these files first:",
        ])
        for f in evidence.relevant_files:
            sections.append(f"- {f}")
        sections.append("")

    if evidence.git_context:
        sections.extend([
            "## Recent Git Context",
            evidence.git_context,
            "",
        ])

    if test_command:
        sections.extend([
            "## Test Command",
            f"Run after fixing: `{test_command}`",
            "",
        ])

    sections.extend([
        "## Constraints",
        "- Fix ONLY the error described. Do not address other issues you notice. One bug, one fix.",
        "- Do NOT modify test expectations or delete tests (DP-04).",
        "- Do NOT modify files outside the project directory.",
        "- Check if the fix already exists in a recent commit before writing new code.",
        "- Keep changes surgical: modify as few files and lines as possible.",
    ])

    return "\n".join(sections)


def _get_git_diff(cwd: str) -> tuple[str, list[str], int]:
    """Get git diff summary. Returns (diff_text, files_changed, lines_changed)."""
    try:
        diff = subprocess.run(
            ["git", "diff", "--stat"],
            capture_output=True, text=True, timeout=10, cwd=cwd,
        )
        diff_names = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True, text=True, timeout=10, cwd=cwd,
        )
        diff_numstat = subprocess.run(
            ["git", "diff", "--numstat"],
            capture_output=True, text=True, timeout=10, cwd=cwd,
        )
        files = [f for f in diff_names.stdout.strip().splitlines() if f]
        total_lines = 0
        for line in diff_numstat.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                try:
                    total_lines += int(parts[0]) + int(parts[1])
                except ValueError:
                    pass
        return diff.stdout[:2000], files, total_lines
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "", [], 0


def _check_test_modifications(cwd: str, files_changed: list[str]) -> list[str]:
    """Check for suspicious test file modifications (G8/F020 defense)."""
    failures = []
    test_files = [f for f in files_changed if TEST_FILE_PATTERNS.search(f)]
    if not test_files:
        return failures

    for tf in test_files:
        try:
            diff = subprocess.run(
                ["git", "diff", "--", tf],
                capture_output=True, text=True, timeout=10, cwd=cwd,
            )
            diff_text = diff.stdout
            # Count removed assertions
            removed_asserts = len(re.findall(r"^-.*(?:assert|expect|should)", diff_text, re.MULTILINE))
            added_asserts = len(re.findall(r"^\+.*(?:assert|expect|should)", diff_text, re.MULTILINE))
            if removed_asserts > added_asserts:
                failures.append(
                    f"Test file {tf}: {removed_asserts} assertions removed, {added_asserts} added (F020)"
                )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    return failures


def verify_fix(evidence: EvidenceBundle, cwd: str) -> list[str]:
    """Post-fix verification checklist (Amendment 7).

    Returns list of verification failures. Empty = all checks passed.
    """
    failures = []
    diff_text, files_changed, lines_changed = _get_git_diff(cwd)

    # 1. Git diff is non-empty
    if not files_changed:
        failures.append("No files changed — agent did not produce a fix")
        return failures

    # 2. Changed files overlap with relevant_files (G5/F002 defense)
    if evidence.relevant_files:
        overlap = set(files_changed) & set(evidence.relevant_files)
        if not overlap:
            failures.append(
                f"Changed files {files_changed} don't overlap with relevant_files "
                f"{evidence.relevant_files} — possible confabulation (F002)"
            )

    # 3. Test file modification check (G8/F020)
    test_failures = _check_test_modifications(cwd, files_changed)
    failures.extend(test_failures)

    # 4. Surgical fix guard
    if len(files_changed) > MAX_FILES_CHANGED:
        failures.append(f"Too many files changed: {len(files_changed)} > {MAX_FILES_CHANGED}")
    if lines_changed > MAX_LINES_CHANGED:
        failures.append(f"Too many lines changed: {lines_changed} > {MAX_LINES_CHANGED}")

    return failures


def spawn_fix_agent(
    evidence: EvidenceBundle,
    config: dict[str, Any],
    project_dir: Path | None = None,
) -> RemediationResult:
    """Spawn a fix agent to diagnose and fix the error.

    Args:
        evidence: The assembled evidence bundle.
        config: Remediation config dict.
        project_dir: Working directory for the fix agent.

    Returns:
        RemediationResult with status and diagnostics.
    """
    timeout = config.get("timeout_seconds", 300)
    cwd = str(project_dir) if project_dir else evidence.project_dir
    if not cwd:
        return RemediationResult(
            attempt_id=evidence.attempt_id,
            status="failed",
            output="No project directory specified",
        )

    # Build prompt
    prompt = build_fix_prompt(evidence)

    # Write prompt to disk for audit trail
    prompt_dir = Path(cwd) / ".opentriage" / "remediations" / evidence.attempt_id
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / "prompt.md"
    prompt_path.write_text(prompt)

    # Check if claude is available
    try:
        subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, OSError):
        return RemediationResult(
            attempt_id=evidence.attempt_id,
            status="agent_unavailable",
            output="claude CLI not found in PATH",
        )

    # Capture pre-fix git state
    pre_status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, timeout=10, cwd=cwd,
    )

    # Spawn agent
    start = time.time()
    try:
        result = subprocess.run(
            ["claude", "--print", "-p", prompt],
            capture_output=True, text=True,
            timeout=timeout, cwd=cwd,
        )
        duration = time.time() - start
        exit_code = result.returncode
        output = (result.stdout + result.stderr)[:4000]
    except subprocess.TimeoutExpired:
        duration = time.time() - start
        return RemediationResult(
            attempt_id=evidence.attempt_id,
            status="timeout",
            duration_seconds=duration,
            output=f"Agent timed out after {timeout}s",
        )
    except (FileNotFoundError, OSError) as e:
        return RemediationResult(
            attempt_id=evidence.attempt_id,
            status="agent_unavailable",
            output=str(e),
        )

    # Post-fix analysis
    diff_text, files_changed, lines_changed = _get_git_diff(cwd)

    # Run verification
    verification_failures = verify_fix(evidence, cwd)

    # Determine status
    if verification_failures:
        status = "suspicious"
    elif exit_code == 0 and files_changed:
        status = "fixed"
    elif not files_changed:
        status = "failed"
    else:
        status = "failed"

    # Auto-commit uncommitted changes if agent succeeded but didn't commit
    commit_sha = None
    if files_changed and status in ("fixed", "suspicious"):
        try:
            subprocess.run(["git", "add", "-A"], capture_output=True, timeout=10, cwd=cwd)
            commit_result = subprocess.run(
                ["git", "commit", "-m", f"[opentriage-autofix] {evidence.attempt_id}"],
                capture_output=True, text=True, timeout=10, cwd=cwd,
            )
            if commit_result.returncode == 0:
                sha_result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True, text=True, timeout=5, cwd=cwd,
                )
                commit_sha = sha_result.stdout.strip()[:12]
        except (subprocess.TimeoutExpired, OSError):
            pass

    # Run tests if test_command specified
    test_output = ""
    test_command = (evidence.remedy or {}).get("test_command", "")
    if test_command and status == "fixed":
        try:
            test_result = subprocess.run(
                test_command.split(),
                capture_output=True, text=True,
                timeout=120, cwd=cwd,
            )
            test_output = (test_result.stdout + test_result.stderr)[:2000]
            if test_result.returncode != 0:
                status = "failed"
                verification_failures.append("Tests failed after fix")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            test_output = f"Test execution error: {e}"

    rem_result = RemediationResult(
        attempt_id=evidence.attempt_id,
        status=status,
        exit_code=exit_code,
        output=output,
        git_diff=diff_text,
        files_changed=files_changed,
        lines_changed=lines_changed,
        test_output=test_output,
        verification_failures=verification_failures,
        duration_seconds=duration,
        commit_sha=commit_sha,
    )

    # Write result to disk
    result_path = prompt_dir / "result.json"
    result_path.write_text(json.dumps(rem_result.to_dict(), indent=2, default=str))

    return rem_result
