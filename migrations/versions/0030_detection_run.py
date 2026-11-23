"""background load-type detection run (progressive bulk detection)

Revision ID: 0030_detection_run
Revises: 0029_compare_key_detection
Create Date: 2026-11-07

Backs progressive bulk load-type detection: a detection_run row accumulates per-table results +
progress so detecting 100+ tables streams into a results table instead of blocking one request.

Defensive + idempotent (golden rule #2): fresh DB builds the whole schema; otherwise the table is
created only if missing.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0030_detection_run"
down_revision = "0029_compare_key_detection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = set(insp.get_table_names())
    if "monitor" not in existing:  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    if "detection_run" not in existing:
        Base.metadata.tables["detection_run"].create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "detection_run" in set(insp.get_table_names()):
        Base.metadata.tables["detection_run"].drop(bind)
