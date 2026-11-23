"""Notifier — delivers report digests to channels (email / Slack / …).

Local/desktop uses `LocalNotifier`, which records deliveries in memory and logs them (no
external calls, so tests exercise the real delivery path without mocks). Server/prod swap in
SMTP / Slack adapters behind the same `send` interface — the dms-notifications service is the
intended production backend (JMS channels: Email, Slack, Teams)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from dvp.core.logging import get_logger

log = get_logger("dvp.notifier")


@dataclass
class Delivery:
    channel: str
    recipient: str
    subject: str
    body: str


class Notifier(Protocol):
    def send(self, channel: str, recipient: str, subject: str, body: str) -> None: ...


class LocalNotifier:
    """Records deliveries in memory + logs them. Used in local mode and tests."""

    def __init__(self) -> None:
        self.sent: list[Delivery] = []

    def send(self, channel: str, recipient: str, subject: str, body: str) -> None:
        self.sent.append(Delivery(channel, recipient, subject, body))
        log.info("notification_sent", channel=channel, recipient=recipient, subject=subject)
