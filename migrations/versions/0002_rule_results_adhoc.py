"""rule_result: allow ad-hoc rule evaluations

Revision ID: 0002_rule_results_adhoc
Revises: 0001_observability_watermark
Create Date: 2026-10-05

Makes ``rule_result.monitor_run_id`` nullable and adds ``rule_result.table_ref`` so a
rule can be evaluated outside a full monitor run (the custom-rule ``:record`` capture
path, docs/design/10 §5.3). Defensive + idempotent (golden rule #2).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0002_rule_results_adhoc"
down_revision = "0001_observability_watermark"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "rule_result" not in insp.get_table_names():
        Base.metadata.create_all(bind)
        return
    cols = {c["name"]: c for c in insp.get_columns("rule_result")}
    with op.batch_alter_table("rule_result") as batch:
        if "table_ref" not in cols:
            batch.add_column(sa.Column("table_ref", sa.String(length=512), nullable=True))
        if "monitor_run_id" in cols and not cols["monitor_run_id"]["nullable"]:
            batch.alter_column("monitor_run_id", existing_type=sa.String(length=36), nullable=True)


def downgrade() -> None:
    # No-op: re-tightening nullability would reject existing ad-hoc rows.
    pass
