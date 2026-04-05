"""Escalation channel implementations."""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Protocol, runtime_checkable
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

log = logging.getLogger(__name__)


@runtime_checkable
class EscalationChannel(Protocol):
    def send(self, alert: dict) -> bool:
        """Send an alert dict. Return True if delivered."""
        ...


class StdoutChannel:
    """Print formatted alerts to stdout."""

    def send(self, alert: dict) -> bool:
        try:
            severity = alert.get("severity", "info").upper()
            title = alert.get("title", "Alert")
            body = alert.get("body", "")
            action = alert.get("action_needed", "")
            print(f"\n{'='*60}")
            print(f"[{severity}] {title}")
            print(f"{'='*60}")
            if body:
                print(body)
            if action:
                print(f"\nAction needed: {action}")
            ctx = alert.get("context", {})
            if ctx:
                print(f"\nContext: {json.dumps(ctx, indent=2)}")
            print(f"{'='*60}\n")
            sys.stdout.flush()
            return True
        except Exception:
            return False


class WebhookChannel:
    """POST JSON to a webhook URL."""

    def __init__(self, url: str) -> None:
        self._url = url

    def send(self, alert: dict) -> bool:
        if not self._url:
            return False
        payload = json.dumps(alert).encode()
        for attempt in range(2):
            try:
                req = Request(
                    self._url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(req, timeout=10) as resp:
                    if 200 <= resp.status < 300:
                        return True
            except (URLError, HTTPError) as e:
                log.warning("Webhook attempt %d failed: %s", attempt + 1, e)
                if attempt == 0:
                    time.sleep(3)
        return False


class DiscordChannel:
    """POST embed to a Discord webhook."""

    def __init__(self, url: str) -> None:
        self._url = url

    def send(self, alert: dict) -> bool:
        if not self._url:
            return False
        body = alert.get("body", "")
        if len(body) > 2000:
            body = body[:1997] + "...[truncated, full alert in .opentriage/escalations.jsonl]"

        embed = {
            "embeds": [{
                "title": alert.get("title", "OpenTriage Alert"),
                "description": body,
                "color": {"critical": 0xFF0000, "high": 0xFF8800, "info": 0x0088FF}.get(
                    alert.get("severity", "info"), 0x888888
                ),
                "fields": [
                    {"name": "Severity", "value": alert.get("severity", "info"), "inline": True},
                    {"name": "Type", "value": alert.get("type", "unknown"), "inline": True},
                ],
                "footer": {"text": f"OpenTriage | {alert.get('ts', '')}"},
            }]
        }
        if alert.get("action_needed"):
            embed["embeds"][0]["fields"].append(
                {"name": "Action Needed", "value": alert["action_needed"]}
            )

        payload = json.dumps(embed).encode()
        for attempt in range(2):
            try:
                req = Request(
                    self._url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(req, timeout=10) as resp:
                    if 200 <= resp.status < 300:
                        return True
                    if resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", "5"))
                        time.sleep(min(retry_after, 10))
            except (URLError, HTTPError) as e:
                log.warning("Discord attempt %d failed: %s", attempt + 1, e)
                if attempt == 0:
                    time.sleep(3)
        return False


class SlackChannel:
    """POST block to a Slack webhook."""

    def __init__(self, url: str) -> None:
        self._url = url

    def send(self, alert: dict) -> bool:
        if not self._url:
            return False
        body = alert.get("body", "")
        if len(body) > 3000:
            body = body[:2997] + "...[truncated, full alert in .opentriage/escalations.jsonl]"

        severity = alert.get("severity", "info")
        emoji = {"critical": ":red_circle:", "high": ":large_orange_circle:", "info": ":large_blue_circle:"}.get(
            severity, ":white_circle:"
        )

        payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": f"{emoji} {alert.get('title', 'Alert')}"},
                },
                {"type": "section", "text": {"type": "mrkdwn", "text": body}},
            ]
        }
        if alert.get("action_needed"):
            payload["blocks"].append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Action needed:* {alert['action_needed']}"},
            })

        data = json.dumps(payload).encode()
        for attempt in range(2):
            try:
                req = Request(
                    self._url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(req, timeout=10) as resp:
                    if 200 <= resp.status < 300:
                        return True
                    if resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", "5"))
                        time.sleep(min(retry_after, 10))
            except (URLError, HTTPError) as e:
                log.warning("Slack attempt %d failed: %s", attempt + 1, e)
                if attempt == 0:
                    time.sleep(3)
        return False
