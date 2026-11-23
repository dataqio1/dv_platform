"""Channel providers + the registry that builds one from an integration's config/secret.

Real transports use only the stdlib (smtplib, urllib) — no new runtime dependency. In local
mode every provider is the RecorderProvider, which routes through the container's LocalNotifier
(records in memory, logs) so desktop users and tests see exactly what *would* be sent.
"""

from __future__ import annotations

import json
import smtplib
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from dvp.core.logging import get_logger

log = get_logger("dvp.integrations")


@dataclass
class DeliveryResult:
    ok: bool
    detail: str


class ChannelProvider(Protocol):
    def send(self, subject: str, body: str, context: dict | None = None) -> DeliveryResult: ...


class RecorderProvider:
    """Local/test sink: record the delivery instead of calling out."""

    def __init__(self, notifier, channel: str, recipient: str) -> None:  # noqa: ANN001
        self.notifier = notifier
        self.channel = channel
        self.recipient = recipient

    def send(self, subject: str, body: str, context: dict | None = None) -> DeliveryResult:
        try:
            if self.notifier is not None:
                self.notifier.send(self.channel, self.recipient, subject, body)
        except Exception as exc:  # noqa: BLE001 - a recorder must never fail the dispatch
            log.warning("recorder_send_failed", error=str(exc))
        return DeliveryResult(True, f"recorded ({self.channel} → {self.recipient})")


def _post_json(url: str, payload: dict, timeout: int = 10) -> int:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-configured URL
        return int(getattr(resp, "status", 0) or 0)


class SlackProvider:
    def __init__(self, webhook_url: str) -> None:
        self.url = webhook_url

    def send(self, subject: str, body: str, context: dict | None = None) -> DeliveryResult:
        if not self.url:
            return DeliveryResult(False, "slack: missing webhook_url")
        try:
            status = _post_json(self.url, {"text": f"*{subject}*\n{body}"})
            return DeliveryResult(200 <= status < 300, f"slack http {status}")
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(False, f"slack error: {exc}")


class TeamsProvider:
    def __init__(self, webhook_url: str) -> None:
        self.url = webhook_url

    def send(self, subject: str, body: str, context: dict | None = None) -> DeliveryResult:
        if not self.url:
            return DeliveryResult(False, "teams: missing webhook_url")
        try:
            card = {
                "@type": "MessageCard",
                "@context": "http://schema.org/extensions",
                "summary": subject,
                "title": subject,
                "text": body,
            }
            status = _post_json(self.url, card)
            return DeliveryResult(200 <= status < 300, f"teams http {status}")
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(False, f"teams error: {exc}")


class WebhookProvider:
    def __init__(self, url: str) -> None:
        self.url = url

    def send(self, subject: str, body: str, context: dict | None = None) -> DeliveryResult:
        if not self.url:
            return DeliveryResult(False, "webhook: missing url")
        try:
            status = _post_json(self.url, {"subject": subject, "body": body})
            return DeliveryResult(200 <= status < 300, f"webhook http {status}")
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(False, f"webhook error: {exc}")


_PD_SEVERITY = {"critical": "critical", "warning": "warning", "info": "info"}


def pagerduty_event(routing_key: str, subject: str, body: str, context: dict | None) -> dict:
    """PagerDuty Events API v2 trigger. dedup_key is the incident, so a repeat collapses."""
    ctx = context or {}
    severity = _PD_SEVERITY.get(str(ctx.get("severity") or "warning").lower(), "error")
    return {
        "routing_key": routing_key,
        "event_action": "trigger",
        "dedup_key": str(ctx.get("incident_id") or subject),
        "payload": {
            "summary": subject[:1024],
            "severity": severity,
            "source": str(ctx.get("table_ref") or "dv-platform"),
            "custom_details": {"body": body},
        },
    }


class PagerDutyProvider:
    def __init__(self, routing_key: str) -> None:
        self.routing_key = routing_key

    def send(self, subject: str, body: str, context: dict | None = None) -> DeliveryResult:
        if not self.routing_key:
            return DeliveryResult(False, "pagerduty: missing routing_key")
        try:
            status = _post_json(
                "https://events.pagerduty.com/v2/enqueue",
                pagerduty_event(self.routing_key, subject, body, context),
            )
            return DeliveryResult(200 <= status < 300, f"pagerduty http {status}")
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(False, f"pagerduty error: {exc}")


class EmailProvider:
    def __init__(
        self, host: str, port: int, sender: str, recipient: str,
        username: str | None, password: str | None, use_tls: bool,
    ) -> None:
        self.host, self.port, self.sender, self.recipient = host, port, sender, recipient
        self.username, self.password, self.use_tls = username, password, use_tls

    def send(self, subject: str, body: str, context: dict | None = None) -> DeliveryResult:
        if not (self.host and self.recipient):
            return DeliveryResult(False, "email: missing smtp_host or recipient")
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = self.sender or self.username or "dvp@localhost"
            msg["To"] = self.recipient
            msg.set_content(body)
            with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                if self.use_tls:
                    smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password or "")
                smtp.send_message(msg)
            return DeliveryResult(True, f"emailed {self.recipient}")
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(False, f"email error: {exc}")


def build_channel_provider(
    provider: str, config: dict | None, secret: dict | None, *, local: bool, notifier=None  # noqa: ANN001
) -> ChannelProvider:
    config = config or {}
    secret = secret or {}
    if local:
        recipient = (
            config.get("to")
            or config.get("channel")
            or config.get("url")
            or secret.get("webhook_url")
            or secret.get("url")
            or provider
        )
        return RecorderProvider(notifier, provider, str(recipient))
    if provider == "slack":
        return SlackProvider(secret.get("webhook_url") or config.get("webhook_url", ""))
    if provider == "teams":
        return TeamsProvider(secret.get("webhook_url") or config.get("webhook_url", ""))
    if provider == "webhook":
        return WebhookProvider(secret.get("url") or config.get("url", ""))
    if provider == "pagerduty":
        return PagerDutyProvider(secret.get("routing_key") or config.get("routing_key", ""))
    if provider == "email":
        return EmailProvider(
            config.get("smtp_host", ""),
            int(config.get("smtp_port", 587) or 587),
            config.get("from", ""),
            config.get("to", ""),
            secret.get("username"),
            secret.get("password"),
            bool(config.get("use_tls", True)),
        )
    raise ValueError(f"unknown channel provider: {provider}")
