"""find column: per-column shape metadata (type/range/length/charset/pattern + low-card set)

Revision ID: 0034_finder_column_shape
Revises: 0033_catalog_cache
Create Date: 2026-11-10

Adds cheap scalar shape metadata to finder_column_profile so a value search can prune columns
(type, numeric range, length range, charset bitmask, format masks) and use an exact distinct-value
set for low-cardinality columns — all BEFORE loading/testing the Bloom filter.

Defensive + idempotent (golden rule #2): fresh DB builds the whole schema; otherwise each new
column is added only if missing (additive, nullable/defaulted — safe on an existing DB).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0034_finder_column_shape"
down_revision = "0033_catalog_cache"
branch_labels = None
depends_on = None

_COLUMNS = [
    ("inferred_type", sa.String(16)),
    ("is_numeric", sa.Boolean()),
    ("is_date", sa.Boolean()),
    ("num_min", sa.Text()),
    ("num_max", sa.Text()),
    ("min_len", sa.Integer()),
    ("max_len", sa.Integer()),
    ("charset_mask", sa.Integer()),
    ("pattern_masks", sa.JSON()),
    ("pattern_exhaustive", sa.Boolean()),
    ("distinct_est", sa.Integer()),
    ("is_low_card", sa.Boolean()),
    ("distinct_values", sa.JSON()),
]


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = set(insp.get_table_names())
    if "monitor" not in existing:  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    if "finder_column_profile" not in existing:
        Base.metadata.tables["finder_column_profile"].create(bind)
        return
    have = {c["name"] for c in insp.get_columns("finder_column_profile")}
    for name, coltype in _COLUMNS:
        if name not in have:
            op.add_column("finder_column_profile", sa.Column(name, coltype, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "finder_column_profile" not in set(insp.get_table_names()):
        return
    have = {c["name"] for c in insp.get_columns("finder_column_profile")}
    for name, _ in _COLUMNS:
        if name in have:
            op.drop_column("finder_column_profile", name)
