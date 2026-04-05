"""Escalation routing — delivers alerts to configured channels."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from opentriage.escalation.channels import (
    DiscordChannel,
    SlackChannel,
    StdoutChannel,
    WebhookChannel,
)
from opentriage.io.writer import write_escalation

log = logging.getLogger(__name__)

MAX_ESCALATIONS_PER_CYCLE = 20


def build_channels(config_esc: dict[str, Any]) -> list[tuple[str, Any]]:
    """Build channel instances from config."""
    channel_names = config_esc.get("channels", ["stdout"])
    channels: list[tuple[str, Any]] = []
    for name in channel_names:
        if name == "stdout":
            channels.append(("stdout", StdoutChannel()))
        elif name == "webhook":
            url = config_esc.get("webhook_url", "")
            if url:
                channels.append(("webhook", WebhookChannel(url)))
        elif name == "discord":
            url = config_esc.get("discord_webhook_url", "")
            if url:
                channels.append(("discord", DiscordChannel(url)))
        elif name == "slack":
            url = config_esc.get("slack_webhook_url", "")
            if url:
                channels.append(("slack", SlackChannel(url)))
    if not channels:
        channels.append(("stdout", StdoutChannel()))
    return channels


def send_alert(
    alert: dict[str, Any],
    config_esc: dict[str, Any],
    opentriage_dir: Path,
) -> None:
    """Send an alert through configured channels and log it."""
    if "ts" not in alert:
        alert["ts"] = time.time()

    channels = build_channels(config_esc)
    delivery_status: dict[str, str] = {}

    for name, channel in channels:
        try:
            ok = channel.send(alert)
            delivery_status[name] = "delivered" if ok else "failed"
            if ok:
                break
        except Exception as e:
            log.warning("Channel %s raised: %s", name, e)
            delivery_status[name] = "error"

    # If all failed, try fallback
    if all(s != "delivered" for s in delivery_status.values()):
        fallback_name = config_esc.get("fallback_channel", "stdout")
        if fallback_name not in delivery_status:
            try:
                fallback = StdoutChannel()
                ok = fallback.send(alert)
                delivery_status[fallback_name] = "delivered" if ok else "failed"
            except Exception:
                delivery_status[fallback_name] = "error"

    # Always log to escalations.jsonl
    alert["delivery_status"] = delivery_status
    write_escalation(opentriage_dir, alert)


class EscalationBatcher:
    """Tracks escalations within a cycle and enforces flood protection."""

    def __init__(self, config_esc: dict[str, Any], opentriage_dir: Path) -> None:
        self._config = config_esc
        self._dir = opentriage_dir
        self._count = 0
        self._overflow: list[dict[str, Any]] = []

    def escalate(self, alert: dict[str, Any]) -> None:
        """Send an alert, respecting the per-cycle limit."""
        if self._count < MAX_ESCALATIONS_PER_CYCLE:
            send_alert(alert, self._config, self._dir)
            self._count += 1
        else:
            self._overflow.append(alert)

    def flush(self) -> None:
        """Send summary for any overflow alerts."""
        if self._overflow:
            summary = {
                "severity": "high",
                "type": "escalation_overflow",
                "title": f"Escalation overflow: {len(self._overflow)} additional alerts batched",
                "body": f"Types: {', '.join(set(a.get('type', '?') for a in self._overflow))}",
                "context": {"overflow_count": len(self._overflow)},
                "action_needed": "Review .opentriage/escalations.jsonl for full details.",
                "ts": time.time(),
            }
            send_alert(summary, self._config, self._dir)
            # Still log all overflow alerts
            for alert in self._overflow:
                alert["delivery_status"] = {"batched": "overflow"}
                write_escalation(self._dir, alert)
            self._overflow.clear()
