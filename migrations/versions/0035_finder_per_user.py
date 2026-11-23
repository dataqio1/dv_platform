"""find column: make the index per-user (search results are private to the user who built it)

Revision ID: 0035_finder_per_user
Revises: 0034_finder_column_shape
Create Date: 2026-11-10

Find Column search is per-user — one user's index must not surface in another user's search,
even within the same group (connections, test cases and observability stay group-level). Adds
`created_by` to the three finder tables and folds it into the uniqueness so two users can each
index the same column.

Defensive + idempotent (golden rule #2): fresh DB builds the whole schema; otherwise columns and
the reworked unique constraints are applied only where missing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0035_finder_per_user"
down_revision = "0034_finder_column_shape"
branch_labels = None
depends_on = None

_TABLES = ("finder_column_profile", "finder_scan_config", "finder_scan_run")
_UNIQUES = {
    "finder_column_profile": (
        "uq_finder_profile",
        ["group_id", "created_by", "connection_id", "schema_name", "table_name", "column_name"],
    ),
    "finder_scan_config": (
        "uq_finder_scan_config",
        ["group_id", "created_by", "connection_id"],
    ),
}


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = set(insp.get_table_names())
    if "monitor" not in existing:  # fresh DB → whole schema (already per-user)
        Base.metadata.create_all(bind)
        return
    for table in _TABLES:
        if table not in existing:
            Base.metadata.tables[table].create(bind)
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        if "created_by" not in cols:
            op.add_column(table, sa.Column("created_by", sa.String(36), nullable=True))
            op.create_index(f"ix_{table}_created_by", table, ["created_by"])
        if table in _UNIQUES:
            name, columns = _UNIQUES[table]
            uniques = {u["name"] for u in insp.get_unique_constraints(table)}
            if name in uniques:
                op.drop_constraint(name, table, type_="unique")
            op.create_unique_constraint(name, table, columns)


def downgrade() -> None:
    pass  # additive / constraint rework — no destructive downgrade
