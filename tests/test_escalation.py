"""Tests for escalation system (F-OT05)."""

import json
import time
from pathlib import Path

from opentriage.escalation.channels import StdoutChannel, EscalationChannel
from opentriage.escalation.router import build_channels, send_alert, EscalationBatcher


def test_stdout_channel_is_escalation_channel():
    ch = StdoutChannel()
    assert isinstance(ch, EscalationChannel)


def test_stdout_channel_send(capsys):
    ch = StdoutChannel()
    alert = {
        "severity": "critical",
        "type": "test",
        "title": "Test Alert",
        "body": "Something happened",
        "action_needed": "Fix it",
        "context": {"key": "value"},
        "ts": time.time(),
    }
    result = ch.send(alert)
    assert result is True
    captured = capsys.readouterr()
    assert "Test Alert" in captured.out
    assert "CRITICAL" in captured.out
    assert "Fix it" in captured.out


def test_build_channels_default():
    channels = build_channels({"channels": ["stdout"]})
    assert len(channels) == 1
    assert channels[0][0] == "stdout"


def test_build_channels_empty_defaults_to_stdout():
    channels = build_channels({})
    assert len(channels) >= 1
    assert channels[0][0] == "stdout"


def test_send_alert_logs_to_file(tmp_dirs):
    ot_dir, _ = tmp_dirs
    alert = {
        "severity": "info",
        "type": "test",
        "title": "Test",
        "body": "Test body",
        "ts": time.time(),
    }
    send_alert(alert, {"channels": ["stdout"]}, ot_dir)

    # Check escalations.jsonl
    esc_path = ot_dir / "escalations.jsonl"
    assert esc_path.exists()
    records = [json.loads(l) for l in esc_path.read_text().splitlines() if l.strip()]
    assert len(records) == 1
    assert records[0]["title"] == "Test"
    assert "delivery_status" in records[0]


def test_escalation_batcher_flood_protection(tmp_dirs):
    ot_dir, _ = tmp_dirs
    batcher = EscalationBatcher({"channels": ["stdout"]}, ot_dir)

    # Send 25 alerts (max 20)
    for i in range(25):
        batcher.escalate({
            "severity": "info",
            "type": "test",
            "title": f"Alert {i}",
            "body": "",
            "ts": time.time(),
        })

    batcher.flush()

    # Check that overflow was handled
    esc_path = ot_dir / "escalations.jsonl"
    records = [json.loads(l) for l in esc_path.read_text().splitlines() if l.strip()]
    # 20 sent + 1 summary + 5 overflow logged
    assert len(records) >= 21


def test_all_channels_fail_uses_fallback(tmp_dirs):
    ot_dir, _ = tmp_dirs
    alert = {
        "severity": "critical",
        "type": "test",
        "title": "Fallback Test",
        "body": "All channels should fail",
        "ts": time.time(),
    }
    # Webhook with invalid URL
    config = {
        "channels": ["webhook"],
        "webhook_url": "",
        "fallback_channel": "stdout",
    }
    send_alert(alert, config, ot_dir)

    # Should still be in escalations.jsonl
    esc_path = ot_dir / "escalations.jsonl"
    assert esc_path.exists()
