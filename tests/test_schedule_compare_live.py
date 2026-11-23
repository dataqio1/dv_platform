"""Recurring (cron) schedules for compare test cases: set/clear via API, and the scheduler
runs them when due. Real API + real scheduler, no mocks."""

from __future__ import annotations

from datetime import UTC, datetime

from dvp.config import get_settings
from dvp.observability.scheduler import Scheduler
from dvp.testing_engine.service import CompareService

API = "/api/v1"

TBL = {
    "columns": [{"name": "ORDER_ID", "data_type": "NUMBER"}, {"name": "AMOUNT", "data_type": "NUMBER"}],
    "rows": [{"ORDER_ID": 1, "AMOUNT": 10}, {"ORDER_ID": 2, "AMOUNT": 20}],
}


def _conn(client, name):
    coords = {"database": "DB", "schema": "PUBLIC", "tables": {"ORDERS": TBL, "ITEMS": TBL}}
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": name, "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]


def _single(client):
    c = _conn(client, "sched-s")
    return client.post(
        f"{API}/compares",
        json={
            "kind": "data", "name": "nightly-check",
            "source": {"connection_id": c, "table": "ORDERS"},
            "target": {"connection_id": c, "table": "ORDERS"},
            "options": {"key": ["ORDER_ID"]},
        },
    ).json()["data"]["id"]


def _ctx_and_scheduler(client):
    container = client.app.state.container
    local = client.app.state.local_tenant
    from dvp.services.tenancy import resolve_context

    ctx = resolve_context(
        container.metadata, user_id=local.user_id, org_id=local.org_id, group_id=local.group_id
    )
    return ctx, Scheduler(container, get_settings(), ctx)


def test_set_and_clear_schedule(client):
    jid = _single(client)
    # Set a daily 02:00 schedule.
    r = client.put(f"{API}/compares/{jid}/schedule", json={"schedule_cron": "0 2 * * *"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["schedule_cron"] == "0 2 * * *"
    assert data["next_run"]  # the next fire time is computed

    # Invalid cron is rejected.
    bad = client.put(f"{API}/compares/{jid}/schedule", json={"schedule_cron": "not a cron"})
    assert bad.status_code >= 400

    # Clearing it returns to manual-only.
    r2 = client.put(f"{API}/compares/{jid}/schedule", json={"schedule_cron": None})
    assert r2.json()["data"]["schedule_cron"] is None
    assert r2.json()["data"]["next_run"] is None


def test_scheduler_runs_a_due_single(client):
    jid = _single(client)
    # Every-minute schedule → always due on the next tick.
    client.put(f"{API}/compares/{jid}/schedule", json={"schedule_cron": "* * * * *"})

    ctx, sched = _ctx_and_scheduler(client)
    ran = sched.run_once(now=datetime.now(UTC))
    assert ran >= 1

    # The job executed (not still 'queued'/'created') — in thread mode it finishes ~instantly.
    import time

    for _ in range(50):
        st = client.get(f"{API}/compares/{jid}").json()["data"]["status"]
        if st in ("succeeded", "failed", "differences"):
            break
        time.sleep(0.1)
    assert client.get(f"{API}/compares/{jid}").json()["data"]["status"] == "succeeded"


def test_scheduler_skips_not_yet_due(client):
    """A schedule that already ran this slot must NOT re-fire until the next slot."""
    jid = _single(client)
    client.put(f"{API}/compares/{jid}/schedule", json={"schedule_cron": "0 2 * * *"})  # daily 02:00

    ctx, sched = _ctx_and_scheduler(client)
    svc = CompareService(
        client.app.state.container.metadata, client.app.state.container.results,
        client.app.state.container.secrets, client.app.state.container.spark,
    )
    # Pretend it just ran at 02:00 today, then evaluate dueness at 02:30 — same cron slot, not due.
    # Stamp finished_at to the simulated slot explicitly: svc.run() records the *real* wall clock,
    # which made this test flaky (it failed whenever real UTC time-of-day was before 02:00).
    now = datetime.now(UTC).replace(hour=2, minute=0, second=0, microsecond=0)
    svc.run(ctx, jid)
    from dvp.db.models import CompareJob

    with client.app.state.container.metadata.session(ctx) as s:
        s.get(CompareJob, jid).finished_at = now
    due = svc.due_schedules(ctx, now.replace(minute=30))
    assert all(ident != jid for _, ident in due)
