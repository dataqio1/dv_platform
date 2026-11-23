"""compare_job.execution_log — timestamped execution trace (viewable/downloadable logs)

Revision ID: 0022_compare_execution_log
Revises: 0021_compare_last_run_by
Create Date: 2026-10-25

Per-execution log (phases, engine, timing, warnings, outcome) stored on the job so it exists
for succeeded AND failed runs and is deleted with the job. Defensive + idempotent (rule #2).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0022_compare_execution_log"
down_revision = "0021_compare_last_run_by"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "compare_job" not in set(insp.get_table_names()):  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    if "execution_log" not in {c["name"] for c in insp.get_columns("compare_job")}:
        op.add_column("compare_job", sa.Column("execution_log", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "compare_job" in set(insp.get_table_names()) and "execution_log" in {
        c["name"] for c in inspect(bind).get_columns("compare_job")
    }:
        op.drop_column("compare_job", "execution_log")
