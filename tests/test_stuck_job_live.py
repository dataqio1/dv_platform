"""A run that fails on bad input must RESOLVE the job (mark it failed + record the run) —
never leave it stuck "running" forever. Real API, no mocks."""

from __future__ import annotations

API = "/api/v1"

TBL = {
    "columns": [{"name": "ORDER_ID", "data_type": "NUMBER"}, {"name": "AMOUNT", "data_type": "NUMBER"}],
    "rows": [{"ORDER_ID": 1, "AMOUNT": 10}, {"ORDER_ID": 2, "AMOUNT": 20}],
}


def _conn(client, name):
    coords = {"database": "DB", "schema": "PUBLIC", "tables": {"ORDERS": TBL}}
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": name, "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]


def test_bad_target_table_marks_failed_not_running(client):
    """A compare pointed at a non-existent table must end 'failed' (NotFoundError), not 'running'."""
    s, t = _conn(client, "s1"), _conn(client, "t1")
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "data", "name": "bad-table",
            "source": {"connection_id": s, "table": "ORDERS"},
            "target": {"connection_id": t, "table": "DOES_NOT_EXIST"},
            "options": {"key": ["ORDER_ID"]},
        },
    ).json()["data"]

    r = client.post(f"{API}/compares/{job['id']}:run")
    assert r.status_code >= 400  # the caller still sees the error

    got = client.get(f"{API}/compares/{job['id']}").json()["data"]
    assert got["status"] == "failed", f"expected failed, got {got['status']}"
    assert got["error"]  # the reason is recorded

    # And it shows up in run history as a failed run (not an invisible phantom). The history
    # groups into single entries ({"run": {...}}) and batch entries ({"runs": [...]}).
    entries = client.get(f"{API}/compares/runs").json()["data"]
    flat = [e["run"] for e in entries if e.get("type") == "single"] + [
        r for e in entries if e.get("type") == "batch" for r in e.get("runs", [])
    ]
    assert any(r.get("job_id") == job["id"] and r["status"] == "failed" for r in flat)


def test_missing_runtime_variable_marks_failed_not_running(client):
    """A {{var}} left unsupplied at run time must resolve the job to 'failed', not strand it."""
    s, t = _conn(client, "s2"), _conn(client, "t2")
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "data", "name": "needs-var",
            "source": {"connection_id": s, "table": "ORDERS"},
            "target": {"connection_id": t, "table": "{{which_table}}"},
            "options": {"key": ["ORDER_ID"]},
        },
    ).json()["data"]

    r = client.post(f"{API}/compares/{job['id']}:run")  # no variables supplied
    assert r.status_code >= 400

    got = client.get(f"{API}/compares/{job['id']}").json()["data"]
    assert got["status"] == "failed"
    assert "which_table" in (got["error"] or "")
