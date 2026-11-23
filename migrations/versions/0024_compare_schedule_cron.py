"""compare_job.schedule_cron — optional recurring schedule for a test case

Revision ID: 0024_compare_schedule_cron
Revises: 0023_compare_run_history
Create Date: 2026-10-27

A test case (or batch) may carry a 5-field cron schedule; the background scheduler re-runs it
when due. None = manual-only. Defensive + idempotent (rule #2).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0024_compare_schedule_cron"
down_revision = "0023_compare_run_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "compare_job" not in set(insp.get_table_names()):  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    if "schedule_cron" not in {c["name"] for c in insp.get_columns("compare_job")}:
        op.add_column("compare_job", sa.Column("schedule_cron", sa.String(length=128), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "compare_job" in set(insp.get_table_names()) and "schedule_cron" in {
        c["name"] for c in inspect(bind).get_columns("compare_job")
    }:
        op.drop_column("compare_job", "schedule_cron")
