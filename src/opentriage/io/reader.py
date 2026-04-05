"""Readers for OpenLog and OpenTriage data files."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file, skipping malformed lines."""
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for i, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("Skipping malformed line %d in %s", i, path)
    return records


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON file. Returns empty dict on parse failure."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        log.warning("Malformed JSON in %s", path)
        return {}


def load_fingerprints(openlog_dir: Path) -> list[dict[str, Any]]:
    """Load fingerprints from .openlog/fingerprints.json."""
    fp_path = openlog_dir / "fingerprints.json"
    data = read_json(fp_path)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "fingerprints" in data:
        return data["fingerprints"]
    return []


def scan_events(
    openlog_dir: Path,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Scan .openlog/events/*.jsonl for error events within the time window.

    Returns events where kind='error' and f_raw is non-empty.
    """
    events_dir = openlog_dir / "events"
    if not events_dir.exists():
        return []

    all_events: list[dict[str, Any]] = []
    for jsonl_file in sorted(events_dir.glob("*.jsonl")):
        for record in read_jsonl(jsonl_file):
            if record.get("kind") != "error":
                continue
            if not record.get("f_raw"):
                continue
            ts = record.get("ts")
            if ts is not None and window_start is not None:
                event_time = datetime.fromtimestamp(ts, tz=timezone.utc)
                if event_time < window_start:
                    continue
                if window_end and event_time > window_end:
                    continue
            # Attach source file info for session tracking
            if "session_id" not in record:
                record["session_id"] = jsonl_file.stem
            all_events.append(record)
    return all_events


def load_correlations(opentriage_dir: Path, date_str: str | None = None) -> list[dict[str, Any]]:
    """Load correlation records. If date_str given, load that day only."""
    corr_dir = opentriage_dir / "correlations"
    if not corr_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    if date_str:
        path = corr_dir / f"{date_str}.jsonl"
        return read_jsonl(path)
    for f in sorted(corr_dir.glob("*.jsonl")):
        records.extend(read_jsonl(f))
    return records


def load_remediations(opentriage_dir: Path, date_str: str | None = None) -> list[dict[str, Any]]:
    """Load remediation records."""
    rem_dir = opentriage_dir / "remediations"
    if not rem_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    if date_str:
        path = rem_dir / f"{date_str}.jsonl"
        return read_jsonl(path)
    for f in sorted(rem_dir.glob("*.jsonl")):
        records.extend(read_jsonl(f))
    return records


def load_escalations(opentriage_dir: Path) -> list[dict[str, Any]]:
    """Load all escalation records."""
    return read_jsonl(opentriage_dir / "escalations.jsonl")


def load_session_events(openlog_dir: Path, session_id: str) -> list[dict[str, Any]]:
    """Load all events from a specific session file."""
    events_dir = openlog_dir / "events"
    if not events_dir.exists():
        return []
    for jsonl_file in events_dir.glob("*.jsonl"):
        if session_id in jsonl_file.stem:
            return read_jsonl(jsonl_file)
    return []
