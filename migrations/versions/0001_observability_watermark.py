"""observability: monitor watermark columns

Revision ID: 0001_observability_watermark
Revises:
Create Date: 2026-10-05

This is the first migration in the scaffold. Local mode builds the schema via
``create_all`` at boot, so this migration exists for server/prod. It is defensive and
idempotent (golden rule #2):

* On a fresh database (no ``monitor`` table) it creates the full ORM schema — which
  already includes the new ``watermark_column`` / ``watermark_type`` columns.
* On an existing database it adds only the two watermark columns, and only if absent.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db.base import Base
from dvp.db import models  # noqa: F401  (ensure all tables are registered on Base)

revision = "0001_observability_watermark"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "monitor" not in insp.get_table_names():
        # Fresh DB — materialise the whole schema (watermark columns included).
        Base.metadata.create_all(bind)
        return

    existing = {c["name"] for c in insp.get_columns("monitor")}
    with op.batch_alter_table("monitor") as batch:
        if "watermark_column" not in existing:
            batch.add_column(sa.Column("watermark_column", sa.String(length=255), nullable=True))
        if "watermark_type" not in existing:
            batch.add_column(sa.Column("watermark_type", sa.String(length=50), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "monitor" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("monitor")}
    with op.batch_alter_table("monitor") as batch:
        if "watermark_type" in existing:
            batch.drop_column("watermark_type")
        if "watermark_column" in existing:
            batch.drop_column("watermark_column")
