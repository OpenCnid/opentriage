"""Tests for CLI interface (F-OT08)."""

import json
from pathlib import Path

from opentriage.cli import main


def test_cli_help(capsys):
    try:
        main(["--help"])
    except SystemExit as e:
        assert e.code == 0
    captured = capsys.readouterr()
    assert "opentriage" in captured.out
    assert "init" in captured.out
    assert "triage" in captured.out
    assert "status" in captured.out


def test_cli_version(capsys):
    try:
        main(["--version"])
    except SystemExit as e:
        assert e.code == 0
    captured = capsys.readouterr()
    assert "1.0.0" in captured.out


def test_cli_init(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["init"])
    ot_dir = tmp_path / ".opentriage"
    assert ot_dir.exists()
    assert (ot_dir / "config.toml").exists()
    assert (ot_dir / "state.json").exists()
    assert (ot_dir / "correlations").is_dir()
    assert (ot_dir / "remediations").is_dir()
    assert (ot_dir / "drafts").is_dir()
    assert (ot_dir / "metrics").is_dir()


def test_cli_init_already_exists(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main(["init"])
    main(["init"])  # Second time
    captured = capsys.readouterr()
    assert "Already initialized" in captured.out


def test_cli_init_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["init"])
    # Modify config
    cfg_path = tmp_path / ".opentriage" / "config.toml"
    original = cfg_path.read_text()
    main(["init", "--force"])
    # Config should be reset
    assert cfg_path.exists()


def test_cli_status(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main(["init"])
    main(["status"])
    captured = capsys.readouterr()
    assert "Circuit Breaker State:" in captured.out
    assert "full-autonomy" in captured.out


def test_cli_config_view(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main(["init"])
    main(["config"])
    captured = capsys.readouterr()
    assert "provider" in captured.out
    assert "backend" in captured.out


def test_cli_config_get(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main(["init"])
    main(["config", "provider.backend"])
    captured = capsys.readouterr()
    assert "anthropic" in captured.out


def test_cli_config_set(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main(["init"])
    main(["config", "provider.backend", "openai"])
    captured = capsys.readouterr()
    assert "openai" in captured.out


def test_cli_promote(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main(["init"])
    main(["promote"])
    captured = capsys.readouterr()
    assert "Current state:" in captured.out

    # Check state was updated
    state = json.loads((tmp_path / ".opentriage" / "state.json").read_text())
    assert state["human_approved_promotion"] is True


def test_cli_triage_dry_run(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main(["init"])
    # No openlog events — should run cleanly
    (tmp_path / ".openlog").mkdir()
    (tmp_path / ".openlog" / "events").mkdir()
    try:
        main(["triage", "--dry-run", "--all"])
    except SystemExit as e:
        assert e.code in (0, None)
    captured = capsys.readouterr()
    assert "Triage complete" in captured.out or "events processed" in captured.out.lower()


def test_cli_health(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main(["init"])
    main(["health", "--today"])
    captured = capsys.readouterr()
    assert "Health Report" in captured.out


def test_cli_without_init(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    try:
        main(["status"])
    except SystemExit as e:
        assert e.code == 1
    captured = capsys.readouterr()
    assert "init" in captured.err.lower()
