"""compare_job: batch grouping (batch_id, batch_name)

Revision ID: 0004_compare_batches
Revises: 0003_incidents
Create Date: 2026-10-07

Adds batch grouping so many per-table compares run together (continue-on-error →
partial). Defensive + idempotent (golden rule #2).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0004_compare_batches"
down_revision = "0003_incidents"
branch_labels = None
depends_on = None

_COLS = [("batch_id", sa.String(length=36)), ("batch_name", sa.String(length=255))]


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "compare_job" not in insp.get_table_names():
        Base.metadata.create_all(bind)
        return
    existing = {c["name"] for c in insp.get_columns("compare_job")}
    with op.batch_alter_table("compare_job") as batch:
        for name, coltype in _COLS:
            if name not in existing:
                batch.add_column(sa.Column(name, coltype, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "compare_job" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("compare_job")}
    with op.batch_alter_table("compare_job") as batch:
        for name, _ in reversed(_COLS):
            if name in existing:
                batch.drop_column(name)
