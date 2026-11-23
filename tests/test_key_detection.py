"""Submit a data compare WITHOUT a key; the engine detects one at run time and records the outcome
on the test case (so the mapping UI can show 'keys detected in last run on <date>' or 'not
detected'). Real local SQLite connectors, no mocks.
"""

from __future__ import annotations

API = "/api/v1"


def _cols(*specs):
    return [{"name": n, "data_type": t, "nullable": nul} for n, t, nul in specs]


# A keyed table (ORDER_ID is unique) and a fully-duplicated table (no unique key at all).
KEYED = {
    "columns": _cols(("ORDER_ID", "NUMBER", False), ("AMOUNT", "NUMBER", False)),
    "rows": [{"ORDER_ID": 1, "AMOUNT": 10}, {"ORDER_ID": 2, "AMOUNT": 20}, {"ORDER_ID": 3, "AMOUNT": 30}],
}
KEYED_DST = {
    "columns": _cols(("ORDER_ID", "NUMBER", False), ("AMOUNT", "NUMBER", False)),
    "rows": [{"ORDER_ID": 1, "AMOUNT": 10}, {"ORDER_ID": 2, "AMOUNT": 99}, {"ORDER_ID": 3, "AMOUNT": 30}],
}
NOKEY = {  # every row is identical → no unique key exists
    "columns": _cols(("GRP", "NUMBER", False), ("VAL", "VARCHAR", True)),
    "rows": [{"GRP": 1, "VAL": "x"}, {"GRP": 1, "VAL": "x"}],
}


def _conn(client, name, table, spec):
    coords = {"database": "DB", "schema": "PUBLIC", "tables": {table: spec}}
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": name, "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]


def _create(client, scid, dcid, table, options=None):
    return client.post(f"{API}/compares", json={
        "kind": "data", "name": "dq",
        "source": {"connection_id": scid, "table": table},
        "target": {"connection_id": dcid, "table": table},
        "options": options or {},
    }).json()["data"]


def test_submit_without_key_then_auto_detected_at_run(client):
    scid = _conn(client, "s1", "ORDERS", KEYED)
    dcid = _conn(client, "d1", "ORDERS", KEYED_DST)
    job = _create(client, scid, dcid, "ORDERS")  # NO options.key
    assert job["status"] == "queued"
    # Submitting without a key is allowed; detection has not run yet.
    assert job["key_status"] is None and job["detected_key"] == []

    client.post(f"{API}/compares/{job['id']}:run")
    done = client.get(f"{API}/compares/{job['id']}").json()["data"]
    assert done["status"] == "succeeded"
    assert done["key_status"] == "detected"
    assert done["detected_key"] == ["ORDER_ID"]
    assert done["key_detected_at"]  # timestamp of the last run's detection, for "detected on <date>"


def test_explicit_key_is_recorded_as_user_provided(client):
    scid = _conn(client, "s2", "ORDERS", KEYED)
    dcid = _conn(client, "d2", "ORDERS", KEYED_DST)
    job = _create(client, scid, dcid, "ORDERS", options={"key": ["ORDER_ID"]})
    client.post(f"{API}/compares/{job['id']}:run")
    done = client.get(f"{API}/compares/{job['id']}").json()["data"]
    assert done["key_status"] == "user"
    assert done["detected_key"] == ["ORDER_ID"]


def test_no_key_detectable_is_handled_and_status_shown(client):
    scid = _conn(client, "s3", "DUP", NOKEY)
    dcid = _conn(client, "d3", "DUP", NOKEY)
    job = _create(client, scid, dcid, "DUP")  # no key, none detectable
    client.post(f"{API}/compares/{job['id']}:run")  # fails gracefully (400) — not a cryptic crash

    done = client.get(f"{API}/compares/{job['id']}").json()["data"]
    # The run-time detection outcome is persisted so the UI can show "not detected on <date>".
    assert done["key_status"] == "not_detected"
    assert done["detected_key"] == []
    assert done["key_detected_at"]
    assert done["status"] == "failed"
    assert "unique key" in (done["error"] or "").lower()
