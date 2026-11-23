"""incident triage — comments, disposition, and excludable metric points

Revision ID: 0017_incident_triage
Revises: 0016_metrics
Create Date: 2026-10-18

Adds: incident_comment (who/when notes), incident.resolution + resolved_by (okay /
false-positive / confirmed), and metric.excluded (a user can drop a wrong data point —
e.g. a double-load spike or a missed load — from charts and baselines).
Defensive + idempotent (golden rule #2).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0017_incident_triage"
down_revision = "0016_metrics"
branch_labels = None
depends_on = None


def _cols(insp, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    if "incident" not in tables:  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    inc_cols = _cols(insp, "incident")
    if "resolution" not in inc_cols:
        op.add_column("incident", sa.Column("resolution", sa.String(20), nullable=True))
    if "resolved_by" not in inc_cols:
        op.add_column("incident", sa.Column("resolved_by", sa.String(36), nullable=True))
    if "excluded" not in _cols(insp, "metric"):
        op.add_column(
            "metric", sa.Column("excluded", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    if "incident_comment" not in tables:
        Base.metadata.tables["incident_comment"].create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    if "incident_comment" in tables:
        Base.metadata.tables["incident_comment"].drop(bind)
    if "incident" in tables:
        cols = _cols(insp, "incident")
        if "resolved_by" in cols:
            op.drop_column("incident", "resolved_by")
        if "resolution" in cols:
            op.drop_column("incident", "resolution")
    if "metric" in tables and "excluded" in _cols(insp, "metric"):
        op.drop_column("metric", "excluded")
