"""compare failure-growth trend — per-run params + normalized failed count

Revision ID: 0028_compare_failure_trend
Revises: 0027_metadata_conventions
Create Date: 2026-11-06

Backs the "+/- failure growth over time" feature for Data Quality test cases: each CompareRun now
records the runtime variables it used, a stable `param_hash` of them (so the trend only compares
like-for-like parameter sets), and a normalized `failed_count` of failed records.

Defensive + idempotent (golden rule #2): fresh DB builds the whole schema; otherwise each column is
added only if missing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0028_compare_failure_trend"
down_revision = "0027_metadata_conventions"
branch_labels = None
depends_on = None

_COLUMNS = {
    "variables": sa.Column("variables", sa.JSON(), nullable=True),
    "param_hash": sa.Column("param_hash", sa.String(64), nullable=True),
    "failed_count": sa.Column("failed_count", sa.Integer(), nullable=True),
}


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = set(insp.get_table_names())
    if "monitor" not in existing:  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    if "compare_run" not in existing:
        return
    have = {c["name"] for c in insp.get_columns("compare_run")}
    for name, column in _COLUMNS.items():
        if name not in have:
            op.add_column("compare_run", column)
    if "param_hash" not in have:
        op.create_index("ix_compare_run_param_hash", "compare_run", ["param_hash"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "compare_run" not in set(insp.get_table_names()):
        return
    have = {c["name"] for c in insp.get_columns("compare_run")}
    idx = {i["name"] for i in insp.get_indexes("compare_run")}
    if "ix_compare_run_param_hash" in idx:
        op.drop_index("ix_compare_run_param_hash", "compare_run")
    for name in _COLUMNS:
        if name in have:
            op.drop_column("compare_run", name)
