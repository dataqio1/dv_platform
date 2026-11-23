"""integrations — tenant-configured outbound channels + routing + delivery log

Revision ID: 0012_integrations
Revises: 0011_incident_monitor_nullable
Create Date: 2026-10-17

Per-tenant notification channels (email/slack/teams/webhook) and ticketing targets
(jira/rally), the routes that fire them on events, and a delivery log (history + dedup).
Secrets live in the SecretStore — only secret_ref pointers are stored here.
Defensive + idempotent (golden rule #2).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0012_integrations"
down_revision = "0011_incident_monitor_nullable"
branch_labels = None
depends_on = None

_TABLES = ("integration", "notification_route", "notification_delivery")


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = set(insp.get_table_names())
    if "monitor" not in existing:  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    for name in _TABLES:
        if name not in existing:
            Base.metadata.tables[name].create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = set(insp.get_table_names())
    for name in reversed(_TABLES):
        if name in existing:
            Base.metadata.tables[name].drop(bind)
