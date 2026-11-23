"""observability UI backend — incident silence + org settings + service accounts

Revision ID: 0020_observability_ui_backend
Revises: 0019_monitor_alert_policy
Create Date: 2026-10-20

Backs the data_observability admin UI:
* incident silence fields (suppress alerting until a deadline, with who/why/ticket),
* org_setting — namespaced JSON settings blobs (organization / auth / governance),
* service_account — non-human identities with hashed API keys.

Defensive + idempotent (golden rule #2): fresh DB builds the whole schema; otherwise each
column/table is added only if missing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0020_observability_ui_backend"
down_revision = "0019_monitor_alert_policy"
branch_labels = None
depends_on = None

_INCIDENT_COLUMNS = {
    "silenced_until": sa.Column("silenced_until", sa.DateTime(timezone=True), nullable=True),
    "silenced_by": sa.Column("silenced_by", sa.String(36), nullable=True),
    "silence_scope": sa.Column("silence_scope", sa.String(20), nullable=True),
    "silence_note": sa.Column("silence_note", sa.Text(), nullable=True),
    "ticket_url": sa.Column("ticket_url", sa.String(512), nullable=True),
}
_TABLES = ("org_setting", "service_account")


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = set(insp.get_table_names())
    if "monitor" not in existing:  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    incident_cols = {c["name"] for c in insp.get_columns("incident")}
    for name, column in _INCIDENT_COLUMNS.items():
        if name not in incident_cols:
            op.add_column("incident", column)
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
    if "incident" in existing:
        incident_cols = {c["name"] for c in insp.get_columns("incident")}
        for name in _INCIDENT_COLUMNS:
            if name in incident_cols:
                op.drop_column("incident", name)
