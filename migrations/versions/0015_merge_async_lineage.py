"""merge the two concurrent heads so `alembic upgrade head` has a single tip

Revision ID: 0015_merge_async_lineage
Revises: 0013_compare_job_async, 0014_group_sample_store
Create Date: 2026-10-17

Two migration branches grew from 0011 in parallel:
  0012_integrations → 0013_compare_job_async        (out-of-process executor work)
  0012_monitor_run_query_shape → 0013_lineage_edge → 0014_group_sample_store  (observability work)
This is a no-op merge that unifies them. No schema changes.
"""

from __future__ import annotations

revision = "0015_merge_async_lineage"
down_revision = ("0013_compare_job_async", "0014_group_sample_store")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
