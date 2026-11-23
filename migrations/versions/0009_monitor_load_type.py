"""monitor.load_type — truncate-and-load vs incremental vs append

Revision ID: 0009_monitor_load_type
Revises: 0008_group_ldap
Create Date: 2026-10-11

Records how each monitored table is loaded so the UI can show it and volume anomaly
detection can reason about expected behavior. Defensive + idempotent (golden rule #2).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0009_monitor_load_type"
down_revision = "0008_group_ldap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "monitor" not in set(insp.get_table_names()):
        Base.metadata.create_all(bind)
        return
    existing = {c["name"] for c in insp.get_columns("monitor")}
    if "load_type" not in existing:
        with op.batch_alter_table("monitor") as batch:
            batch.add_column(
                sa.Column("load_type", sa.String(length=20), nullable=False,
                          server_default="incremental")
            )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "monitor" in set(insp.get_table_names()):
        existing = {c["name"] for c in insp.get_columns("monitor")}
        if "load_type" in existing:
            with op.batch_alter_table("monitor") as batch:
                batch.drop_column("load_type")
