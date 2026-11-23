"""lineage_edge — parent table to child table

Revision ID: 0013_lineage_edge
Revises: 0012_monitor_run_query_shape
Create Date: 2026-10-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0013_lineage_edge"
down_revision = "0012_monitor_run_query_shape"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "lineage_edge" in set(insp.get_table_names()):
        return
    if "monitor" not in set(insp.get_table_names()):
        Base.metadata.create_all(bind)
        return
    op.create_table(
        "lineage_edge",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("upstream_ref", sa.String(length=512), nullable=False),
        sa.Column("downstream_ref", sa.String(length=512), nullable=False),
        sa.Column("org_id", sa.String(length=36), nullable=False),
        sa.Column("group_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("group_id", "upstream_ref", "downstream_ref", name="uq_lineage_edge"),
    )
    op.create_index("ix_lineage_edge_org_id", "lineage_edge", ["org_id"])
    op.create_index("ix_lineage_edge_group_id", "lineage_edge", ["group_id"])


def downgrade() -> None:
    pass
