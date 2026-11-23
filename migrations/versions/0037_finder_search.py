"""persist Find Column searches (per-user history with scan/latency metadata + delete)

Revision ID: 0037_finder_search
Revises: 0036_project_variables
Create Date: 2026-11-12

Adds the `finder_search` table so a user can see their recent Find Column searches — the value(s)
looked for, scope, how much was scanned (tables/columns/connections), matches, and latency — and
re-open or delete them. Per-user (created_by), consistent with the rest of Find Column.

Defensive + idempotent (golden rule #2): a fresh DB builds the whole schema; otherwise the table is
created only if missing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0037_finder_search"
down_revision = "0036_project_variables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = set(insp.get_table_names())
    if "finder_search" in existing:
        return
    if "connection" not in existing:  # fresh DB → build the whole schema
        Base.metadata.create_all(bind)
        return
    op.create_table(
        "finder_search",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), index=True),
        sa.Column("group_id", sa.String(36), index=True),
        sa.Column("created_by", sa.String(36), index=True, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("values", sa.JSON()),
        sa.Column("match_mode", sa.String(16)),
        sa.Column("scope_label", sa.String(512), nullable=True),
        sa.Column("connection_ids", sa.JSON(), nullable=True),
        sa.Column("schemas", sa.JSON(), nullable=True),
        sa.Column("scanned_connections", sa.Integer()),
        sa.Column("scanned_tables", sa.Integer()),
        sa.Column("scanned_columns", sa.Integer()),
        sa.Column("matched_tables", sa.Integer()),
        sa.Column("total_matches", sa.Integer()),
        sa.Column("took_ms", sa.Float()),
        sa.Column("used_index", sa.Boolean()),
        sa.Column("truncated", sa.Boolean()),
        sa.Column("result", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "finder_search" in set(insp.get_table_names()):
        op.drop_table("finder_search")
