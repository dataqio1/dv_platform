"""monitor.lookback_days — how far back a monitor analyzes data (baseline window)

Revision ID: 0018_monitor_lookback
Revises: 0017_incident_triage
Create Date: 2026-10-19

The analysis/history window a monitor uses for baselines and charts. Defaults to 30 days.
Defensive + idempotent (golden rule #2).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0018_monitor_lookback"
down_revision = "0017_incident_triage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "monitor" not in set(insp.get_table_names()):  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    if "lookback_days" not in {c["name"] for c in insp.get_columns("monitor")}:
        op.add_column(
            "monitor",
            sa.Column("lookback_days", sa.Integer(), nullable=False, server_default="30"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "monitor" in set(insp.get_table_names()) and "lookback_days" in {
        c["name"] for c in insp.get_columns("monitor")
    }:
        op.drop_column("monitor", "lookback_days")
