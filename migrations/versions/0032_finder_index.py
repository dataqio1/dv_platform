"""find column (value locator) index: per-column Bloom profiles + scan config/runs

Revision ID: 0032_finder_index
Revises: 0031_feedback
Create Date: 2026-11-10

Backs the Find Column index: `finder_column_profile` stores a per-column Bloom filter (plus
row/null counts and min/max) so a value search can shortlist candidate columns without scanning
the source; `finder_scan_config` drives daily/incremental refresh; `finder_scan_run` records
each build for status/history.

Defensive + idempotent (golden rule #2): fresh DB builds the whole schema; otherwise each table
is created only if missing.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0032_finder_index"
down_revision = "0031_feedback"
branch_labels = None
depends_on = None

_TABLES = ("finder_column_profile", "finder_scan_config", "finder_scan_run")


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = set(insp.get_table_names())
    if "monitor" not in existing:  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    for name in _TABLES:
        if name not in existing:
            Base.metadata.tables[name].create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = set(insp.get_table_names())
    for name in reversed(_TABLES):
        if name in existing:
            Base.metadata.tables[name].drop(bind)
