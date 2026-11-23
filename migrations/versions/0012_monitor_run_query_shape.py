"""monitor_run.query_shape — check versus a full column profile

Revision ID: 0012_monitor_run_query_shape
Revises: 0011_incident_monitor_nullable
Create Date: 2026-10-17

A scheduled check and a full column profile write different query shapes.
The column is what a later cost view sums.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0012_monitor_run_query_shape"
down_revision = "0011_incident_monitor_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "monitor_run" not in set(insp.get_table_names()):
        Base.metadata.create_all(bind)
        return
    have = {c["name"] for c in insp.get_columns("monitor_run")}
    if "query_shape" in have:
        return
    op.add_column("monitor_run", sa.Column("query_shape", sa.String(length=32), nullable=True))


def downgrade() -> None:
    pass
