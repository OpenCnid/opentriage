"""Writers for OpenTriage data files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append a single JSON record to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write a JSON file atomically (write to tmp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.rename(path)


def write_correlation(opentriage_dir: Path, record: dict[str, Any]) -> None:
    """Write a correlation record to the appropriate date file."""
    from datetime import datetime, timezone
    ts = record.get("ts", datetime.now(timezone.utc).timestamp())
    date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    path = opentriage_dir / "correlations" / f"{date_str}.jsonl"
    append_jsonl(path, record)


def write_remediation(opentriage_dir: Path, record: dict[str, Any]) -> None:
    """Write a remediation record to the appropriate date file."""
    from datetime import datetime, timezone
    ts = record.get("ts", datetime.now(timezone.utc).timestamp())
    date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    path = opentriage_dir / "remediations" / f"{date_str}.jsonl"
    append_jsonl(path, record)


def write_escalation(opentriage_dir: Path, record: dict[str, Any]) -> None:
    """Append an escalation record."""
    path = opentriage_dir / "escalations.jsonl"
    append_jsonl(path, record)


def write_draft(opentriage_dir: Path, slug: str, data: dict[str, Any]) -> None:
    """Write a draft fingerprint to .opentriage/drafts/{slug}.json."""
    path = opentriage_dir / "drafts" / f"{slug}.json"
    write_json(path, data)


def write_metrics(opentriage_dir: Path, date_str: str, data: dict[str, Any]) -> None:
    """Write daily metrics to .opentriage/metrics/{date}.json."""
    path = opentriage_dir / "metrics" / f"{date_str}.json"
    write_json(path, data)


def write_state(opentriage_dir: Path, state: dict[str, Any]) -> None:
    """Write state.json atomically."""
    write_json(opentriage_dir / "state.json", state)
