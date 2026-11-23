"""catalog listing cache (schemas/tables/databases per connection)

Revision ID: 0033_catalog_cache
Revises: 0032_finder_index
Create Date: 2026-11-10

Backs a cache of slow catalog listings so schema/table pickers (and the Find Column table scan)
load instantly, with an explicit refresh to re-fetch from the source.

Defensive + idempotent (golden rule #2): fresh DB builds the whole schema; otherwise the table is
created only if missing.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

from dvp.db import models  # noqa: F401
from dvp.db.base import Base

revision = "0033_catalog_cache"
down_revision = "0032_finder_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = set(insp.get_table_names())
    if "monitor" not in existing:  # fresh DB → whole schema
        Base.metadata.create_all(bind)
        return
    if "catalog_cache" not in existing:
        Base.metadata.tables["catalog_cache"].create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "catalog_cache" in set(insp.get_table_names()):
        Base.metadata.tables["catalog_cache"].drop(bind)
