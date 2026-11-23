"""compare_result: runtime variables used for the execution

Revision ID: 0005_compare_variables
Revises: 0004_compare_batches
Create Date: 2026-10-07

Records the resolved ``{{var}}`` values used for each execution. Idempotent (golden rule #2).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0005_compare_variables"
down_revision = "0004_compare_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "compare_result" not in insp.get_table_names():
        Base.metadata.create_all(bind)
        return
    existing = {c["name"] for c in insp.get_columns("compare_result")}
    if "variables" not in existing:
        with op.batch_alter_table("compare_result") as batch:
            batch.add_column(sa.Column("variables", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "compare_result" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("compare_result")}
    if "variables" in existing:
        with op.batch_alter_table("compare_result") as batch:
            batch.drop_column("variables")
