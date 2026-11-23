"""user feedback (per-page feedback widget → admin review)

Revision ID: 0031_feedback
Revises: 0030_detection_run
Create Date: 2026-11-09

Backs the in-app feedback widget: any user can submit feedback from any page; admins review it
in Admin → Feedback. We persist the page + browser context alongside the message.

Defensive + idempotent (golden rule #2): fresh DB builds the whole schema; otherwise the table
is created only if missing.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0031_feedback"
down_revision = "0030_detection_run"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = set(insp.get_table_names())
    if "monitor" not in existing:  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    if "feedback" not in existing:
        Base.metadata.tables["feedback"].create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "feedback" in set(insp.get_table_names()):
        Base.metadata.tables["feedback"].drop(bind)
