"""compare key-detection outcome — submit without a key, record detection per run

Revision ID: 0029_compare_key_detection
Revises: 0028_compare_failure_trend
Create Date: 2026-11-07

A data compare can be submitted without a key; the engine detects one at run time. This records the
per-run outcome on the test case so the mapping UI can show "keys detected in last run on <date>"
(or "not detected"): compare_job gains key_status, detected_key, key_detected_at.

Defensive + idempotent (golden rule #2): fresh DB builds the whole schema; otherwise each column is
added only if missing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0029_compare_key_detection"
down_revision = "0028_compare_failure_trend"
branch_labels = None
depends_on = None

_COLUMNS = {
    "key_status": sa.Column("key_status", sa.String(20), nullable=True),
    "detected_key": sa.Column("detected_key", sa.JSON(), nullable=True),
    "key_detected_at": sa.Column("key_detected_at", sa.DateTime(timezone=True), nullable=True),
}


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "compare_job" not in set(insp.get_table_names()):  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    have = {c["name"] for c in insp.get_columns("compare_job")}
    for name, column in _COLUMNS.items():
        if name not in have:
            op.add_column("compare_job", column)


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "compare_job" not in set(insp.get_table_names()):
        return
    have = {c["name"] for c in insp.get_columns("compare_job")}
    for name in _COLUMNS:
        if name in have:
            op.drop_column("compare_job", name)
