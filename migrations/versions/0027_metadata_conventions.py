"""named ETL metadata conventions + multi-convention attachment

Revision ID: 0027_metadata_conventions
Revises: 0026_load_type_scd2_provenance
Create Date: 2026-11-06

Backs docs/design/11 §3.4: named, reusable metadata-column conventions (an org library) that many
connections reference, and a connection loaded by >1 ETL tool attaches several. Adds:
* metadata_convention — the user-defined convention library (shipped presets stay in code),
* connection.metadata_convention_ids / monitor.metadata_convention_ids — the attachments.

Defensive + idempotent (golden rule #2): fresh DB builds the whole schema; otherwise the table and
columns are added only if missing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0027_metadata_conventions"
down_revision = "0026_load_type_scd2_provenance"
branch_labels = None
depends_on = None

_COLUMNS = {
    "connection": {"metadata_convention_ids": sa.Column("metadata_convention_ids", sa.JSON(), nullable=True)},
    "monitor": {"metadata_convention_ids": sa.Column("metadata_convention_ids", sa.JSON(), nullable=True)},
}


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = set(insp.get_table_names())
    if "monitor" not in existing:  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    if "metadata_convention" not in existing:
        Base.metadata.tables["metadata_convention"].create(bind)
    for table, cols in _COLUMNS.items():
        if table not in existing:
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        for name, column in cols.items():
            if name not in have:
                op.add_column(table, column)


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = set(insp.get_table_names())
    for table, cols in _COLUMNS.items():
        if table not in existing:
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        for name in cols:
            if name in have:
                op.drop_column(table, name)
    if "metadata_convention" in existing:
        Base.metadata.tables["metadata_convention"].drop(bind)
