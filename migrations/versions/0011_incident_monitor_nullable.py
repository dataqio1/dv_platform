"""incident.monitor_id nullable — a compare can open an incident on its own

Revision ID: 0011_incident_monitor_nullable
Revises: 0010_table_metadata
Create Date: 2026-10-17

A failed compare that is not started from a monitored table still needs an incident.
That incident has no monitor.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0011_incident_monitor_nullable"
down_revision = "0010_table_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "incident" not in set(insp.get_table_names()):
        Base.metadata.create_all(bind)
        return
    col = next((c for c in insp.get_columns("incident") if c["name"] == "monitor_id"), None)
    if col is None or col.get("nullable"):
        return
    with op.batch_alter_table("incident") as batch:
        batch.alter_column(
            "monitor_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )


def downgrade() -> None:
    pass
