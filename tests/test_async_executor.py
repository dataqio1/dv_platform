"""Out-of-process compare execution: DB-as-queue + a worker that runs each compare as its
own OS process (no Redis, no K8s). Uses a shared *file* SQLite so the spawned process sees
the same DB as the test (in-memory SQLite is per-process and can't be shared).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dvp.abstractions.factory import build_container
from dvp.config import get_settings
from dvp.services.connections import ConnectionService
from dvp.services.tenancy import bootstrap_local_tenant, resolve_context
from dvp.testing_engine.service import CompareService
from dvp.workers.queue import JobQueueStore
from dvp.workers.worker import Worker

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _inline(n: int) -> dict:
    return {
        "database": "DB",
        "schema": "PUBLIC",
        "tables": {
            "ORDERS": {
                "columns": [
                    {"name": "ID", "data_type": "NUMBER"},
                    {"name": "AMOUNT", "data_type": "NUMBER"},
                ],
                "rows": [{"ID": i, "AMOUNT": i * 10} for i in range(n)],
            }
        },
    }


@pytest.fixture
def file_env(tmp_path, monkeypatch):
    db = tmp_path / "dvp.db"
    monkeypatch.setenv("DVP_DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("DVP_PROFILE", "local")
    monkeypatch.setenv("DVP_COMPARE_EXECUTOR", "subprocess")
    monkeypatch.setenv("DVP_EMBED_WORKER", "false")
    monkeypatch.setenv("PYTHONPATH", SRC)  # so the spawned `python -m dvp.workers.run_compare` imports dvp
    get_settings.cache_clear()
    settings = get_settings()
    container = build_container(settings)
    container.metadata.create_all()
    local = bootstrap_local_tenant(container.metadata, settings)
    ctx = resolve_context(
        container.metadata, user_id=local.user_id, org_id=local.org_id, group_id=local.group_id
    )
    yield container, ctx
    get_settings.cache_clear()


def _make_compare(container, ctx) -> str:
    conns = ConnectionService(container.metadata, container.secrets)
    src = conns.create(ctx, source_type="local", name="src", coordinates=_inline(3), auth_method="none")["id"]
    dst = conns.create(ctx, source_type="local", name="dst", coordinates=_inline(2), auth_method="none")["id"]
    svc = CompareService(container.metadata, container.results, container.secrets, container.spark)
    job = svc.create_job(
        ctx,
        kind="count",
        name="cmp",
        source={"connection_id": src, "schema": "PUBLIC", "table": "ORDERS"},
        target={"connection_id": dst, "schema": "PUBLIC", "table": "ORDERS"},
        options={},
    )
    return job["id"]


def test_compare_executes_in_a_separate_process(file_env):
    container, ctx = file_env
    svc = CompareService(container.metadata, container.results, container.secrets, container.spark)
    jid = _make_compare(container, ctx)

    JobQueueStore(container.metadata).enqueue(ctx, jid, {})
    assert svc.get_job(ctx, jid)["status"] == "queued"  # not run inline

    # The worker claims it and spawns `python -m dvp.workers.run_compare` as its own process.
    Worker(container, worker_id="test").drain(timeout=90)

    assert svc.get_job(ctx, jid)["status"] == "succeeded"


def test_claim_is_atomic_and_single_assignment(file_env):
    container, ctx = file_env
    jid = _make_compare(container, ctx)
    q = JobQueueStore(container.metadata)
    q.enqueue(ctx, jid, {})

    first = q.claim_next("worker-a")
    assert first is not None and first["id"] == jid
    # now 'running' → a second worker gets nothing
    assert q.claim_next("worker-b") is None
    assert q.snapshot(jid)["status"] == "running"


def test_fail_stale_marks_hung_jobs_failed_and_never_reruns(file_env):
    container, ctx = file_env
    jid = _make_compare(container, ctx)
    q = JobQueueStore(container.metadata)
    q.enqueue(ctx, jid, {})
    q.claim_next("worker-a")  # becomes running with a fresh heartbeat

    # Backdate the heartbeat to simulate a crashed worker / hung run, then sweep.
    from datetime import timedelta

    from dvp.db.base import utcnow
    from dvp.db.models import CompareJob

    with container.metadata.session() as s:
        s.get(CompareJob, jid).heartbeat_at = utcnow() - timedelta(hours=1)

    failed = q.fail_stale(timeout_seconds=60)
    # Once failed, it stays failed — the job is NOT requeued, so it can never auto-rerun.
    assert jid in failed
    snap = q.snapshot(jid)
    assert snap["status"] == "failed"
    assert snap["error"]
    # A failed job is not claimable by any worker.
    assert q.claim_next("worker-b") is None
