"""monitor.alert_policy — per-monitor freshness cadence/grace + per-pillar severity thresholds

Revision ID: 0019_monitor_alert_policy
Revises: 0018_monitor_lookback
Create Date: 2026-10-19

One alert policy per monitor, applied identically to every table and file in it. Holds the
file-arrival freshness source + cadence/grace and the warn/severe thresholds per pillar.
JSON column defaulting to an empty object ("use engine defaults"). Idempotent (golden rule #2).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0019_monitor_alert_policy"
down_revision = "0018_monitor_lookback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "monitor" not in set(insp.get_table_names()):  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    if "alert_policy" not in {c["name"] for c in insp.get_columns("monitor")}:
        op.add_column(
            "monitor",
            sa.Column("alert_policy", sa.JSON(), nullable=False, server_default="{}"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "monitor" in set(insp.get_table_names()) and "alert_policy" in {
        c["name"] for c in insp.get_columns("monitor")
    }:
        op.drop_column("monitor", "alert_policy")
