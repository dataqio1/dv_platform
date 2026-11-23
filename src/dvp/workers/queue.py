"""JobQueueStore — the metadata DB used as a durable work queue (no broker).

`enqueue` is tenant-scoped (the API marks a caller's compare job queued). `claim_next`,
`heartbeat`, and `fail_stale` run at the system level (an unscoped session) because a
worker serves every tenant; on Postgres it uses `FOR UPDATE SKIP LOCKED` so many workers
claim safely, on SQLite the single-writer transaction serializes the claim. In Postgres+RLS
deployments the worker connects with a BYPASSRLS/service role so it can see all tenants.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from dvp.core.errors import NotFoundError
from dvp.core.tenancy import TenantContext
from dvp.db.base import utcnow
from dvp.db.models import CompareJob
from dvp.db.session import MetadataStore


class JobQueueStore:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    # ----- producer (tenant-scoped) -----
    def enqueue(self, ctx: TenantContext, job_id: str, variables: dict | None = None) -> str:
        with self.store.session(ctx) as s:
            job = s.get(CompareJob, job_id)
            if job is None or job.group_id != ctx.group_id:
                raise NotFoundError("compare job not found", details={"id": job_id})
            job.status = "queued"
            job.variables = variables or {}
            job.worker_id = None
            job.started_at = None
            job.finished_at = None
            job.heartbeat_at = None
            job.error = None
            return job.id

    # ----- consumer (system level) -----
    def claim_next(self, worker_id: str, per_group_cap: int | None = None) -> dict | None:
        """Atomically claim one queued job for this worker, respecting an optional per-group
        running cap (so one tenant can't monopolise the pool). Returns a snapshot or None."""
        with self.store.session() as s:
            stmt = (
                select(CompareJob)
                .where(CompareJob.status == "queued")
                .order_by(CompareJob.created_at)
            )
            if s.bind.dialect.name == "postgresql":
                stmt = stmt.with_for_update(skip_locked=True)
            for job in s.scalars(stmt.limit(25)):
                if per_group_cap is not None:
                    running = s.scalar(
                        select(func.count())
                        .select_from(CompareJob)
                        .where(
                            CompareJob.group_id == job.group_id,
                            CompareJob.status == "running",
                        )
                    )
                    if (running or 0) >= per_group_cap:
                        continue
                now = utcnow()
                job.status = "running"
                job.worker_id = worker_id
                job.started_at = now
                job.heartbeat_at = now
                s.flush()
                return {
                    "id": job.id,
                    "org_id": job.org_id,
                    "group_id": job.group_id,
                    "created_by": job.created_by,
                    "variables": dict(job.variables or {}),
                }
            return None

    def heartbeat(self, job_id: str) -> None:
        with self.store.session() as s:
            job = s.get(CompareJob, job_id)
            if job is not None and job.status == "running":
                job.heartbeat_at = utcnow()

    def fail_stale(self, timeout_seconds: int) -> list[str]:
        """Terminate jobs stuck 'running' past the timeout (worker died, or the run hung) →
        status='failed'. We deliberately do NOT requeue: once a run fails it stays failed, so a
        hung/erroring compare can never silently auto-rerun in a loop."""
        cutoff = utcnow() - timedelta(seconds=timeout_seconds)
        with self.store.session() as s:
            stale = s.scalars(
                select(CompareJob).where(
                    CompareJob.status == "running",
                    CompareJob.heartbeat_at.is_not(None),
                    CompareJob.heartbeat_at < cutoff,
                )
            ).all()
            for job in stale:
                job.status = "failed"
                job.worker_id = None
                job.finished_at = utcnow()
                job.error = f"timed out: no heartbeat for over {timeout_seconds}s (worker lost or run hung)"
            return [j.id for j in stale]

    def mark_failed(self, job_id: str, error: str) -> None:
        with self.store.session() as s:
            job = s.get(CompareJob, job_id)
            if job is not None and job.status != "succeeded":
                job.status = "failed"
                job.error = error
                job.finished_at = utcnow()

    def snapshot(self, job_id: str) -> dict | None:
        with self.store.session() as s:
            job = s.get(CompareJob, job_id)
            if job is None:
                return None
            return {
                "id": job.id,
                "status": job.status,
                "group_id": job.group_id,
                "org_id": job.org_id,
                "created_by": job.created_by,
                "error": job.error,
            }
