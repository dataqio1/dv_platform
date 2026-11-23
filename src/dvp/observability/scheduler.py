"""A minimal background scheduler for local mode.

When enabled (``DVP_SCHEDULER_ENABLED=true``), a daemon thread wakes every
``scheduler_interval_seconds`` and runs monitors whose schedule is due.
A monitor with no schedule stays manual and is never run from this loop.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from sqlalchemy import func, select

from dvp.abstractions.factory import Container
from dvp.config import Settings
from dvp.core.cron import previous_due
from dvp.core.logging import get_logger
from dvp.core.tenancy import TenantContext
from dvp.db.models import CompareJob, Monitor, MonitorRun
from dvp.observability.runner import MonitorRunner

log = get_logger("dvp.scheduler")


class Scheduler:
    def __init__(self, container: Container, settings: Settings, ctx: TenantContext) -> None:
        self.container = container
        self.settings = settings
        self.ctx = ctx
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="dvp-scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler_started", interval_s=self.settings.scheduler_interval_seconds)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        # First tick after one interval (let the app finish booting).
        while not self._stop.wait(self.settings.scheduler_interval_seconds):
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - never let the loop die
                log.warning("scheduler_tick_failed", error=str(exc))

    def run_once(self, now: datetime | None = None) -> int:
        """Run active monitors whose schedule has come due. Manual monitors stay put."""
        now = now or datetime.now(UTC)
        runner = MonitorRunner(self.container.metadata, self.container.secrets)
        with self.container.metadata.session(self.ctx) as s:
            monitors = s.scalars(
                select(Monitor).where(
                    Monitor.group_id == self.ctx.group_id,
                    Monitor.status == "active",
                    Monitor.schedule_cron.is_not(None),
                )
            ).all()
            lasts = dict(
                s.execute(
                    select(MonitorRun.monitor_id, func.max(MonitorRun.finished_at)).where(
                        MonitorRun.group_id == self.ctx.group_id,
                        MonitorRun.monitor_id.in_([m.id for m in monitors] or [""]),
                    ).group_by(MonitorRun.monitor_id)
                ).all()
            )
            due = []
            for mon in monitors:
                slot = previous_due(mon.schedule_cron or "", now)
                last = lasts.get(mon.id)
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                if slot is not None and (last is None or last < slot):
                    due.append(mon.id)
        ran = 0
        for mid in due:
            try:
                summary = runner.run_monitor(self.ctx, mid)
                ran += 1
                log.info("scheduler_ran_monitor", monitor=mid, rules=summary["rules_evaluated"])
            except Exception as exc:  # noqa: BLE001
                log.warning("scheduler_monitor_failed", monitor=mid, error=str(exc))

        # Also run due historical-baseline metrics (same cron semantics).
        try:
            from dvp.observability.metrics import MetricService

            metrics_ran = MetricService(self.container.metadata, self.container.secrets).run_due(
                self.ctx, now
            )
            if metrics_ran:
                ran += metrics_ran
                log.info("scheduler_ran_metrics", count=metrics_ran)
        except Exception as exc:  # noqa: BLE001
            log.warning("scheduler_metrics_failed", error=str(exc))

        # And re-run due compare test cases (single or batch) on the worker tier (never inline —
        # a heavy compare must not block the scheduler loop).
        try:
            ran += self._run_due_compares(now)
        except Exception as exc:  # noqa: BLE001
            log.warning("scheduler_compares_failed", error=str(exc))

        # Refresh any Find Column indexes whose daily schedule has come due (at most once a day,
        # enforced by how next_run_at is advanced).
        try:
            from dvp.services.finder_index import FinderIndexService

            idx_ran = FinderIndexService(self.container.metadata, self.container.secrets).refresh_due(
                self.ctx, now
            )
            if idx_ran:
                ran += idx_ran
                log.info("scheduler_refreshed_finder_index", count=idx_ran)
        except Exception as exc:  # noqa: BLE001
            log.warning("scheduler_finder_index_failed", error=str(exc))
        return ran

    def _run_due_compares(self, now: datetime) -> int:
        """Enqueue every compare test case whose cron schedule has come due, as the worker tier
        does for a manual run — so scheduled and manual runs share the same resource-aware queue."""
        import uuid

        from dvp.testing_engine.service import CompareService

        svc = CompareService(
            self.container.metadata, self.container.results, self.container.secrets,
            self.container.spark, cancel_registry=self.container.cancel_registry,
        )
        subprocess = getattr(self.container.settings, "compare_executor", "thread") == "subprocess"
        svc.active_runs = None if subprocess else getattr(self.container, "active_runs", None)

        due = svc.due_schedules(self.ctx, now)
        ran = 0
        for kind, ident in due:
            try:
                if kind == "batch":
                    jobs = svc.requeue_batch(self.ctx, ident)
                    brid = str(uuid.uuid4())
                    for jid, variables in jobs:
                        self._enqueue(svc, jid, {**(variables or {}), "__batch_run_id__": brid})
                    log.info("scheduler_ran_compare_batch", batch=ident, pairs=len(jobs))
                else:
                    with self.container.metadata.session(self.ctx) as s:
                        j = s.get(CompareJob, ident)
                        stored_vars = dict(j.variables or {}) if j else {}
                    self._enqueue(svc, ident, stored_vars)
                    log.info("scheduler_ran_compare", job=ident)
                ran += 1
            except Exception as exc:  # noqa: BLE001 - one bad schedule can't stop the rest
                log.warning("scheduler_compare_failed", id=ident, kind=kind, error=str(exc))
        return ran

    def _enqueue(self, svc, job_id: str, variables: dict) -> None:  # noqa: ANN001
        """Hand a scheduled compare to the worker tier — mirrors the router's _enqueue."""
        if getattr(self.container.settings, "compare_executor", "thread") == "subprocess":
            from dvp.workers.queue import JobQueueStore

            JobQueueStore(self.container.metadata).enqueue(self.ctx, job_id, variables)
        else:
            mem, cpu = svc.mark_queued(self.ctx, job_id)
            self.container.active_runs.add(job_id)
            self.container.jobs.submit(
                svc.run_safe, self.ctx, job_id, variables, memory_gb=mem, vcpus=cpu
            )
