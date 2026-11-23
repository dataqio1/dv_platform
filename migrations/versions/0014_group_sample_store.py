"""group.sample_store — where compare exception rows are written

Revision ID: 0014_group_sample_store
Revises: 0013_lineage_edge
Create Date: 2026-10-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0014_group_sample_store"
down_revision = "0013_lineage_edge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "group" not in set(insp.get_table_names()):
        return
    have = {c["name"] for c in insp.get_columns("group")}
    if "sample_store" in have:
        return
    op.add_column("group", sa.Column("sample_store", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "group" not in set(insp.get_table_names()):
        return
    have = {c["name"] for c in insp.get_columns("group")}
    if "sample_store" not in have:
        return
    op.drop_column("group", "sample_store")
