"""Remediation handlers — subprocess, callback, noop (F-OT04)."""

from __future__ import annotations

import logging
import subprocess
import tempfile
from typing import Any, Callable

log = logging.getLogger(__name__)


def execute_subprocess(
    command_template: str,
    event: dict[str, Any],
    fingerprint: dict[str, Any],
    remedy_context: str,
    timeout_seconds: int = 300,
) -> tuple[int, str]:
    """Execute remedy via subprocess with shell=False.

    Returns (exit_code, output). exit_code -1 means spawn failed.
    """
    # Write remedy context to temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, prefix="ot_remedy_") as f:
        f.write(remedy_context)
        remedy_file = f.name

    # Fill template variables
    cmd_str = command_template.format(
        event_id=event.get("ref", "unknown"),
        session_id=event.get("session_id", "unknown"),
        remedy_file=remedy_file,
        fingerprint_slug=fingerprint.get("slug", "unknown"),
    )

    args = cmd_str.split()
    if not args:
        return -1, "Empty command template"

    try:
        result = subprocess.run(
            args,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        output = result.stdout + result.stderr
        return result.returncode, output[:2000]
    except FileNotFoundError as e:
        log.error("Command not found: %s", e)
        return -1, f"spawn_failed: {e}"
    except subprocess.TimeoutExpired:
        log.error("Remediation timed out after %ds", timeout_seconds)
        return -2, "timeout"
    except Exception as e:
        log.error("Remediation subprocess error: %s", e)
        return -1, str(e)


def execute_callback(
    callback: Callable,
    event: dict[str, Any],
    fingerprint: dict[str, Any],
    remedy_context: str,
) -> tuple[int, str]:
    """Execute remedy via Python callback."""
    try:
        result = callback(event, fingerprint, remedy_context)
        return 0, str(result) if result else "ok"
    except Exception as e:
        log.error("Callback error: %s", e)
        return 1, str(e)


def execute_noop(
    event: dict[str, Any],
    fingerprint: dict[str, Any],
    remedy_context: str,
) -> tuple[int, str]:
    """Log the remediation without executing."""
    log.info(
        "NOOP: Would remediate %s (fingerprint: %s) with: %s",
        event.get("ref", "?"),
        fingerprint.get("slug", "?"),
        remedy_context[:200],
    )
    return 0, "noop"


def build_remedy_context(
    event: dict[str, Any],
    fingerprint: dict[str, Any],
) -> str:
    """Build the remedy context string written to the temp file."""
    remedy = fingerprint.get("remedy", "")
    return f"""Remedy: {remedy}

Original error (f_raw): {event.get('f_raw', '')}

Stderr (first 500 chars): {str(event.get('stderr', ''))[:500]}

Fingerprint slug: {fingerprint.get('slug', '')}

Previous run failed with the above pattern. Apply the documented remedy."""
