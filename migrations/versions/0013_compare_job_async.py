"""compare_job async executor columns — DB-as-queue for out-of-process compares

Revision ID: 0013_compare_job_async
Revises: 0012_integrations
Create Date: 2026-10-17

Adds the bookkeeping a `dvp worker` needs to claim and run each heavy compare as its own
OS process: stored runtime variables, the claiming worker id, start + heartbeat times.
Defensive + idempotent (golden rule #2).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0013_compare_job_async"
down_revision = "0012_integrations"
branch_labels = None
depends_on = None

_COLUMNS = {
    "variables": sa.Column("variables", sa.JSON(), nullable=True),
    "worker_id": sa.Column("worker_id", sa.String(64), nullable=True),
    "started_at": sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    "heartbeat_at": sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
}


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "compare_job" not in set(insp.get_table_names()):  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    existing = {c["name"] for c in insp.get_columns("compare_job")}
    for name, col in _COLUMNS.items():
        if name not in existing:
            op.add_column("compare_job", col)


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "compare_job" not in set(insp.get_table_names()):
        return
    existing = {c["name"] for c in insp.get_columns("compare_job")}
    for name in reversed(list(_COLUMNS)):
        if name in existing:
            op.drop_column("compare_job", name)
