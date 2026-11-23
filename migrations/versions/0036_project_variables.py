"""project-level variables (a base layer of {{vars}} inherited by every test case in the project)

Revision ID: 0036_project_variables
Revises: 0035_finder_per_user
Create Date: 2026-11-10

Lets a project define {{variables}} once (e.g. env, run_date) that every test-case run in the
project inherits — overridden by job-level and run-time values.

Defensive + idempotent (golden rule #2): fresh DB builds the whole schema; otherwise the column
is added only if missing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0036_project_variables"
down_revision = "0035_finder_per_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = set(insp.get_table_names())
    if "project" not in existing:  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    cols = {c["name"] for c in insp.get_columns("project")}
    if "variables" not in cols:
        op.add_column("project", sa.Column("variables", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "project" in set(insp.get_table_names()):
        cols = {c["name"] for c in insp.get_columns("project")}
        if "variables" in cols:
            op.drop_column("project", "variables")
