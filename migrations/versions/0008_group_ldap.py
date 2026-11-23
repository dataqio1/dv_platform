"""group.ldap_group — bind a tenant group to its LDAP source group

Revision ID: 0008_group_ldap
Revises: 0007_projects_and_report_versions
Create Date: 2026-10-08

Each tenant can be controlled by an LDAP group (JIT provisioning). Defensive + idempotent
(golden rule #2).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0008_group_ldap"
down_revision = "0007_projects_and_report_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "group" not in set(insp.get_table_names()):
        Base.metadata.create_all(bind)
        return
    existing = {c["name"] for c in insp.get_columns("group")}
    if "ldap_group" not in existing:
        with op.batch_alter_table("group") as batch:
            batch.add_column(sa.Column("ldap_group", sa.String(length=512), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "group" in set(insp.get_table_names()):
        existing = {c["name"] for c in insp.get_columns("group")}
        if "ldap_group" in existing:
            with op.batch_alter_table("group") as batch:
                batch.drop_column("ldap_group")
