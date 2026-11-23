"""projects + report versioning + test-case project/source/layer tagging

Revision ID: 0007_projects_and_report_versions
Revises: 0006_reports
Create Date: 2026-10-08

Adds:
  * `project` table (groups test cases)
  * `report_version` table (immutable report snapshots)
  * project_id / source_system / layer / tags on `compare_job`
  * project_id / source_system / layer on `monitor`
  * project_id / latest_version / schedule_cron / delivery on `report`

Defensive + idempotent (golden rule #2): fresh DB → create everything; else add only what's
missing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0007_projects_and_report_versions"
down_revision = "0006_reports"
branch_labels = None
depends_on = None

_COMPARE_COLS = [
    ("project_id", sa.String(length=36)),
    ("source_system", sa.String(length=128)),
    ("layer", sa.String(length=32)),
    ("tags", sa.JSON()),
]
_MONITOR_COLS = [
    ("project_id", sa.String(length=36)),
    ("source_system", sa.String(length=128)),
    ("layer", sa.String(length=32)),
]
_REPORT_COLS = [
    ("project_id", sa.String(length=36)),
    ("latest_version", sa.Integer()),
    ("schedule_cron", sa.String(length=100)),
    ("delivery", sa.JSON()),
]


def _add_missing(insp, table: str, cols: list) -> None:  # noqa: ANN001
    existing = {c["name"] for c in insp.get_columns(table)}
    with op.batch_alter_table(table) as batch:
        for name, coltype in cols:
            if name not in existing:
                batch.add_column(sa.Column(name, coltype, nullable=True))


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    if "compare_job" not in tables:  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    for tbl in ("tag", "project", "report_version"):
        if tbl not in tables:
            Base.metadata.tables[tbl].create(bind)
    _add_missing(insp, "compare_job", _COMPARE_COLS)
    _add_missing(insp, "monitor", _MONITOR_COLS)
    if "report" in tables:
        _add_missing(insp, "report", _REPORT_COLS)


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    for tbl in ("report_version", "project", "tag"):
        if tbl in tables:
            Base.metadata.tables[tbl].drop(bind)
    for table, cols in (("compare_job", _COMPARE_COLS), ("monitor", _MONITOR_COLS)):
        if table in tables:
            existing = {c["name"] for c in insp.get_columns(table)}
            with op.batch_alter_table(table) as batch:
                for name, _ in reversed(cols):
                    if name in existing:
                        batch.drop_column(name)
