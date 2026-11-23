"""incidents: dedup / dimension / kind / evidence / occurrences / resolved_at

Revision ID: 0003_incidents
Revises: 0002_rule_results_adhoc
Create Date: 2026-10-07

Adds signal-quality fields to `incident` (fingerprint-based dedup, dimension, kind,
evidence JSON, occurrence count, resolved_at). Defensive + idempotent (golden rule #2).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0003_incidents"
down_revision = "0002_rule_results_adhoc"
branch_labels = None
depends_on = None

_COLS = [
    ("dedup_key", sa.String(length=255)),
    ("dimension", sa.String(length=32)),
    ("kind", sa.String(length=32)),
    ("evidence", sa.JSON()),
    ("occurrences", sa.Integer()),
    ("resolved_at", sa.DateTime(timezone=True)),
]


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "incident" not in insp.get_table_names():
        Base.metadata.create_all(bind)
        return
    existing = {c["name"] for c in insp.get_columns("incident")}
    with op.batch_alter_table("incident") as batch:
        for name, coltype in _COLS:
            if name not in existing:
                batch.add_column(sa.Column(name, coltype, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "incident" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("incident")}
    with op.batch_alter_table("incident") as batch:
        for name, _ in reversed(_COLS):
            if name in existing:
                batch.drop_column(name)
