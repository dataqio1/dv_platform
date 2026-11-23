"""Outbound integrations — notification channels (email/slack/teams/webhook) and, later,
ticketing (jira/rally). Tenant-scoped: each group configures its own.

Local/desktop + tests record deliveries via the container's LocalNotifier (no network),
so the full dispatch path is exercised without mocks. Server/prod use the real stdlib
transports in `providers` (SMTP / HTTP POST) — no third-party dependencies.
"""
