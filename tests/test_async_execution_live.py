"""Async test-case execution on the worker tier (real API, no mocks).

Submitting many test cases must NOT run them inline on the request path — `:submit` returns
202 immediately with status=queued; the bounded worker pool drains them. We poll until done.
"""

from __future__ import annotations

import time

API = "/api/v1"

TBL = {
    "columns": [
        {"name": "ORDER_ID", "data_type": "NUMBER"},
        {"name": "AMOUNT", "data_type": "NUMBER"},
    ],
    "rows": [{"ORDER_ID": 1, "AMOUNT": 10}, {"ORDER_ID": 2, "AMOUNT": 20}],
}


def _conn(client, name):
    coords = {"database": "DB", "schema": "PUBLIC", "tables": {"ORDERS": TBL}}
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": name, "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]


def _make_job(client):
    s, t = _conn(client, "s"), _conn(client, "t")
    return client.post(
        f"{API}/compares",
        json={
            "kind": "data",
            "name": "cmp",
            "source": {"connection_id": s, "table": "ORDERS"},
            "target": {"connection_id": t, "table": "ORDERS"},
            "options": {"key": ["ORDER_ID"]},
        },
    ).json()["data"]["id"]


def _await_status(client, job_id, *, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = client.get(f"{API}/compares/{job_id}").json()["data"]
        if d["status"] in ("succeeded", "failed"):
            return d
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in time")


def test_submit_returns_immediately_and_runs_async(client):
    job_id = _make_job(client)
    r = client.post(f"{API}/compares/{job_id}:submit")
    assert r.status_code == 202
    body = r.json()["data"]
    assert body["status"] == "queued" and body["job_id"] == job_id

    done = _await_status(client, job_id)
    assert done["status"] == "succeeded"
    assert done["result"]["summary"]["matched_rows"] == 2


def test_many_submissions_all_complete(client):
    # 10 test cases submitted at once — the API accepts all immediately; the bounded pool drains them
    job_ids = [_make_job(client) for _ in range(10)]
    for jid in job_ids:
        assert client.post(f"{API}/compares/{jid}:submit").status_code == 202
    for jid in job_ids:
        assert _await_status(client, jid)["status"] == "succeeded"


def test_batch_submit_async(client):
    s = _conn(client, "bs")
    t = _conn(client, "bt")
    r = client.post(
        f"{API}/compares/batch:submit",
        json={
            "kind": "data",
            "name": "nightly",
            "source": {"connection_id": s, "schema": "PUBLIC"},
            "target": {"connection_id": t, "schema": "PUBLIC"},
            "tables": ["ORDERS"],
            "options": {"key": ["ORDER_ID"]},
        },
    )
    assert r.status_code == 202
    data = r.json()["data"]
    assert data["queued"] == 1 and data["batch_id"]
    # the batch drains on the worker tier
    deadline = time.time() + 10
    while time.time() < deadline:
        b = client.get(f"{API}/compares/batches/{data['batch_id']}").json()["data"]
        if b["total"] == 1 and b["succeeded"] + b["failed"] == 1:
            break
        time.sleep(0.05)
    assert b["succeeded"] == 1


def test_submit_bad_job_records_failure(client):
    # a data compare with no key and undetectable key → run_safe must record failed, not crash
    s = _conn(client, "x")
    t = _conn(client, "y")
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "data",
            "name": "bad",
            "source": {"connection_id": s, "sql": "SELECT 1 AS A"},
            "target": {"connection_id": t, "sql": "SELECT 1 AS A"},
            "options": {"variables_required": "{{missing}}"},
        },
    ).json()["data"]
    client.post(f"{API}/compares/{job['id']}:submit")
    done = _await_status(client, job["id"])
    assert done["status"] == "failed" and done["error"]
