"""A server restart must not leave in-flight runs stuck 'running' forever — the startup reaper
sweeps phantom running/queued jobs to failed. Real DB, no mocks."""

from __future__ import annotations

from dvp.db.models import CompareJob
from dvp.testing_engine.service import reap_orphaned_runs

API = "/api/v1"

TBL = {
    "columns": [{"name": "ORDER_ID", "data_type": "NUMBER"}],
    "rows": [{"ORDER_ID": 1}],
}


def _job(client, status: str) -> str:
    """Create a compare and force it into the given in-flight status (simulating a crash mid-run)."""
    cid = client.post(
        f"{API}/connections",
        json={
            "source_type": "local", "name": f"c-{status}", "auth_method": "none",
            "coordinates": {"database": "DB", "schema": "PUBLIC", "tables": {"ORDERS": TBL}},
        },
    ).json()["data"]["id"]
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "data", "name": f"inflight-{status}",
            "source": {"connection_id": cid, "table": "ORDERS"},
            "target": {"connection_id": cid, "table": "ORDERS"},
            "options": {"key": ["ORDER_ID"]},
        },
    ).json()["data"]["id"]
    store = client.app.state.container.metadata
    with store.session(None) as s:
        s.get(CompareJob, job).status = status
    return job


def _status(client, job_id: str) -> str:
    store = client.app.state.container.metadata
    with store.session(None) as s:
        return s.get(CompareJob, job_id).status


def test_reaper_fails_running_and_queued_in_thread_mode(client):
    running = _job(client, "running")
    queued = _job(client, "queued")

    n = reap_orphaned_runs(client.app.state.container.metadata, include_queued=True)
    assert n >= 2

    assert _status(client, running) == "failed"
    assert _status(client, queued) == "failed"

    # The reaped job carries an explanatory error + a recorded run (visible in history).
    got = client.get(f"{API}/compares/{running}").json()["data"]
    assert "restart" in (got["error"] or "").lower()

    entries = client.get(f"{API}/compares/runs").json()["data"]
    flat = [e["run"] for e in entries if e.get("type") == "single"]
    assert any(r.get("job_id") == running and r["status"] == "failed" for r in flat)


def test_reaper_spares_queued_with_durable_queue(client):
    """With a durable queue (include_queued=False), a queued job is left for a worker to pick up."""
    queued = _job(client, "queued")
    reap_orphaned_runs(client.app.state.container.metadata, include_queued=False)
    assert _status(client, queued) == "queued"
