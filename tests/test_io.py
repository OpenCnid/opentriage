"""Tests for I/O layer."""

import json
import time
from pathlib import Path

from opentriage.io.reader import load_fingerprints, read_json, read_jsonl, scan_events
from opentriage.io.writer import append_jsonl, write_correlation, write_json


def test_read_jsonl(tmp_path):
    path = tmp_path / "test.jsonl"
    path.write_text('{"a": 1}\n{"b": 2}\n')
    records = read_jsonl(path)
    assert len(records) == 2
    assert records[0]["a"] == 1


def test_read_jsonl_skips_malformed(tmp_path):
    path = tmp_path / "test.jsonl"
    path.write_text('{"a": 1}\nnot json\n{"b": 2}\n')
    records = read_jsonl(path)
    assert len(records) == 2


def test_read_jsonl_missing_file(tmp_path):
    records = read_jsonl(tmp_path / "nonexistent.jsonl")
    assert records == []


def test_read_json_missing(tmp_path):
    result = read_json(tmp_path / "nope.json")
    assert result == {}


def test_read_json_malformed(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json!!")
    result = read_json(path)
    assert result == {}


def test_write_json_atomic(tmp_path):
    path = tmp_path / "data.json"
    write_json(path, {"key": "value"})
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["key"] == "value"


def test_append_jsonl(tmp_path):
    path = tmp_path / "log.jsonl"
    append_jsonl(path, {"a": 1})
    append_jsonl(path, {"b": 2})
    lines = path.read_text().strip().split("\n")
    assert len(lines) == 2


def test_write_correlation(tmp_path):
    ot_dir = tmp_path / ".opentriage"
    ot_dir.mkdir()
    (ot_dir / "correlations").mkdir()
    write_correlation(ot_dir, {"ts": time.time(), "ref": "t1"})
    files = list((ot_dir / "correlations").glob("*.jsonl"))
    assert len(files) == 1


def test_load_fingerprints_list(tmp_path):
    ol_dir = tmp_path / ".openlog"
    ol_dir.mkdir()
    fps = [{"slug": "test", "patterns": ["test"]}]
    (ol_dir / "fingerprints.json").write_text(json.dumps(fps))
    result = load_fingerprints(ol_dir)
    assert len(result) == 1


def test_load_fingerprints_dict_format(tmp_path):
    ol_dir = tmp_path / ".openlog"
    ol_dir.mkdir()
    data = {"fingerprints": [{"slug": "test"}]}
    (ol_dir / "fingerprints.json").write_text(json.dumps(data))
    result = load_fingerprints(ol_dir)
    assert len(result) == 1


def test_scan_events_filters_errors(tmp_path):
    ol_dir = tmp_path / ".openlog"
    events_dir = ol_dir / "events"
    events_dir.mkdir(parents=True)
    now = time.time()
    events = [
        {"ts": now, "kind": "error", "ref": "t1", "f_raw": "broken"},
        {"ts": now, "kind": "complete", "ref": "t2", "f_raw": ""},
        {"ts": now, "kind": "error", "ref": "t3", "f_raw": ""},  # Empty f_raw
    ]
    path = events_dir / "session.jsonl"
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    result = scan_events(ol_dir)
    assert len(result) == 1  # Only error with non-empty f_raw
    assert result[0]["ref"] == "t1"
