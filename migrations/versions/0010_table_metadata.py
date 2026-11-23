"""table_metadata — per-table load-pattern catalog (injectable from external systems)

Revision ID: 0010_table_metadata
Revises: 0009_monitor_load_type
Create Date: 2026-10-12

Each table's load pattern (append / cdc / full_snapshot / scd2 / partitioned / reference)
drives every pillar. Stored per table so a customer's ingestion software can inject it.
Defensive + idempotent (golden rule #2).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0010_table_metadata"
down_revision = "0009_monitor_load_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    if "monitor" not in tables:  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    if "table_metadata" not in tables:
        Base.metadata.tables["table_metadata"].create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "table_metadata" in set(insp.get_table_names()):
        Base.metadata.tables["table_metadata"].drop(bind)
