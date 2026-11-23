"""compare_run — per-execution history snapshots (every run, not just the latest)

Revision ID: 0023_compare_run_history
Revises: 0022_compare_execution_log
Create Date: 2026-10-27

The Executions view shows every run of a test case, not only its latest state. CompareRun
accumulates one row per execution. Defensive + idempotent (rule #2): create only if absent,
and fall back to a full create_all on a fresh DB.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0023_compare_run_history"
down_revision = "0022_compare_execution_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    if "compare_job" not in tables:  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    if "compare_run" in tables:
        return
    op.create_table(
        "compare_run",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("org_id", sa.String(length=36), nullable=False),
        sa.Column("group_id", sa.String(length=36), nullable=False),
        sa.Column("compare_job_id", sa.String(length=36), sa.ForeignKey("compare_job.id"), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("batch_id", sa.String(length=36), nullable=True),
        sa.Column("batch_name", sa.String(length=255), nullable=True),
        sa.Column("batch_run_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("execution_log", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_compare_run_compare_job_id", "compare_run", ["compare_job_id"])
    op.create_index("ix_compare_run_project_id", "compare_run", ["project_id"])
    op.create_index("ix_compare_run_batch_id", "compare_run", ["batch_id"])
    op.create_index("ix_compare_run_batch_run_id", "compare_run", ["batch_run_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if "compare_run" in set(inspect(bind).get_table_names()):
        op.drop_table("compare_run")
