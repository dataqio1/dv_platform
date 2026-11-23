"""compare_job.last_run_by — who triggered the most recent execution

Revision ID: 0021_compare_last_run_by
Revises: 0020_observability_ui_backend
Create Date: 2026-10-23

Attribution for test-case executions: `created_by` already records the author; this adds
who last ran it (may differ). Defensive + idempotent (golden rule #2).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0021_compare_last_run_by"
down_revision = "0020_observability_ui_backend"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "compare_job" not in set(insp.get_table_names()):  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    if "last_run_by" not in {c["name"] for c in insp.get_columns("compare_job")}:
        op.add_column("compare_job", sa.Column("last_run_by", sa.String(length=36), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "compare_job" in set(insp.get_table_names()) and "last_run_by" in {
        c["name"] for c in insp.get_columns("compare_job")
    }:
        op.drop_column("compare_job", "last_run_by")
