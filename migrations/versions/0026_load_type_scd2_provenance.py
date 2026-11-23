"""load-type detection: SCD2 roles, ETL metadata-column convention, rule provenance

Revision ID: 0026_load_type_scd2_provenance
Revises: 0025_compare_progress
Create Date: 2026-11-05

Backs docs/design/11 (Load-Type Detection & Load-Aware DQ Rules):
* table_metadata — SCD2 end-date role + batch id + business key
    (effective_to_column, batch_id_column, business_key),
* dq_rule / metric_rule — `origin` provenance label (system|suggested|user|injected),
* connection / monitor — `metadata_columns` ETL-convention blob.

Defensive + idempotent (golden rule #2): fresh DB builds the whole schema; otherwise each
column is added only if missing. Existing rows backfill `origin='user'` (nothing auto-generated
them before), so no behavioural change on upgrade.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0026_load_type_scd2_provenance"
down_revision = "0025_compare_progress"
branch_labels = None
depends_on = None

# table → {column_name: Column}
_ADDITIONS: dict[str, dict] = {
    "table_metadata": {
        "effective_to_column": sa.Column("effective_to_column", sa.String(255), nullable=True),
        "batch_id_column": sa.Column("batch_id_column", sa.String(255), nullable=True),
        "business_key": sa.Column("business_key", sa.JSON(), nullable=True),
    },
    "dq_rule": {
        "origin": sa.Column("origin", sa.String(16), nullable=False, server_default="user"),
    },
    "metric_rule": {
        "origin": sa.Column("origin", sa.String(16), nullable=False, server_default="user"),
    },
    "connection": {
        "metadata_columns": sa.Column("metadata_columns", sa.JSON(), nullable=True),
    },
    "monitor": {
        "metadata_columns": sa.Column("metadata_columns", sa.JSON(), nullable=True),
    },
}


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = set(insp.get_table_names())
    if "monitor" not in existing:  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    for table, cols in _ADDITIONS.items():
        if table not in existing:
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        for name, column in cols.items():
            if name not in have:
                op.add_column(table, column)


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = set(insp.get_table_names())
    for table, cols in _ADDITIONS.items():
        if table not in existing:
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        for name in cols:
            if name in have:
                op.drop_column(table, name)
