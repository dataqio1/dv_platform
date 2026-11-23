"""historical-baseline metrics — definitions, rules, observations

Revision ID: 0016_metrics
Revises: 0015_merge_async_lineage
Create Date: 2026-10-17

Metric definitions (SQL/file/dashboard source), their baseline rules, and the permanent
observation history used to compute baselines. Defensive + idempotent (golden rule #2).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0016_metrics"
down_revision = "0015_merge_async_lineage"
branch_labels = None
depends_on = None

_TABLES = ("metric_definition", "metric_rule", "metric_observation")


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
