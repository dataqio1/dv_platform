"""compare_job.progress — live progress for long runs ({pct, processed, total})

Revision ID: 0025_compare_progress
Revises: 0024_compare_schedule_cron
Create Date: 2026-10-27

Streaming compares of large tables can run for a while; this column lets the UI show how far
along a run is. Cleared when the run finishes. Defensive + idempotent (rule #2).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0025_compare_progress"
down_revision = "0024_compare_schedule_cron"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "compare_job" not in set(insp.get_table_names()):  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    if "progress" not in {c["name"] for c in insp.get_columns("compare_job")}:
        op.add_column("compare_job", sa.Column("progress", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "compare_job" in set(insp.get_table_names()) and "progress" in {
        c["name"] for c in inspect(bind).get_columns("compare_job")
    }:
        op.drop_column("compare_job", "progress")
