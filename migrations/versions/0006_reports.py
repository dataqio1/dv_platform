"""report: persisted aggregate reports (data validation / quality / executive)

Revision ID: 0006_reports
Revises: 0005_compare_variables
Create Date: 2026-10-08

Adds the `report` table backing the reporting module (dms-reporting parity). Defensive +
idempotent (golden rule #2): creates the whole schema if the DB is fresh, else creates just
this table when missing.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0006_reports"
down_revision = "0005_compare_variables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    if "compare_job" not in tables:  # fresh DB → create everything
        Base.metadata.create_all(bind)
        return
    if "report" not in tables:
        Base.metadata.tables["report"].create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "report" in set(insp.get_table_names()):
        Base.metadata.tables["report"].drop(bind)
