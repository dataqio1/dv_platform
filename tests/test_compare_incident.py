"""A compare from an incident stays on that incident. Resolve stays human."""

from __future__ import annotations

API = "/api/v1"


def _cols(*specs):
    return [{"name": n, "data_type": t, "nullable": nul} for n, t, nul in specs]


def _table(rows):
    return {
        "columns": _cols(("ORDER_ID", "NUMBER", False), ("AMOUNT", "NUMBER", False)),
        "rows": rows,
    }


def _conn(client, name, rows):
    return client.post(
        f"{API}/connections",
        json={
            "source_type": "local",
            "name": name,
            "auth_method": "none",
            "coordinates": {"database": "DB", "schema": "PUBLIC", "tables": {"ORDERS": _table(rows)}},
        },
    ).json()["data"]["id"]


def _run(client, src, dst, options=None):
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "count",
            "name": "orders parity",
            "source": {"connection_id": src, "database": "DB", "schema": "PUBLIC", "table": "ORDERS"},
            "target": {"connection_id": dst, "database": "DB", "schema": "PUBLIC", "table": "ORDERS"},
            "options": options or {},
        },
    ).json()["data"]
    return client.post(f"{API}/compares/{job['id']}:run").json()["data"]


FULL = [{"ORDER_ID": 1, "AMOUNT": 10}, {"ORDER_ID": 2, "AMOUNT": 20}, {"ORDER_ID": 3, "AMOUNT": 30}]
SHORT = FULL[:2]


def test_a_compare_that_cannot_run_does_not_open_an_incident(client):
    src = client.post(
        f"{API}/connections",
        json={
            "source_type": "inline",
            "name": "src",
            "auth_method": "none",
            "coordinates": {"schema": "PUBLIC", "tables": {"ORDERS": _table(FULL)}},
        },
    ).json()["data"]["id"]
    dst = client.post(
        f"{API}/connections",
        json={
            "source_type": "inline",
            "name": "dst",
            "auth_method": "none",
            "coordinates": {"schema": "PUBLIC", "tables": {"ORDERS": _table(SHORT)}},
        },
    ).json()["data"]["id"]
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "data",
            "name": "cmp-live",
            "source": {"connection_id": src, "schema": "PUBLIC", "table": "ORDERS"},
            "target": {"connection_id": dst, "schema": "PUBLIC", "table": "ORDERS"},
            "options": {"key": ["ORDER_ID"]},
        },
    ).json()["data"]
    ran = client.post(f"{API}/compares/{job['id']}:run").json()["data"]
    assert ran["status"] == "failed"
    assert "does not support arbitrary SQL" in ran["error"]
    assert client.get(f"{API}/incidents").json()["data"] == []


def test_match_does_not_open_an_incident(client):
    src = _conn(client, "src", FULL)
    dst = _conn(client, "dst", FULL)
    _run(client, src, dst)
    assert client.get(f"{API}/incidents").json()["data"] == []


def test_difference_opens_compare_incident_and_a_match_does_not_resolve_it(client):
    src = _conn(client, "src-a", FULL)
    dst = _conn(client, "dst-a", SHORT)
    mid = client.post(
        f"{API}/monitors",
        json={"connection_id": dst, "name": "orders", "scope": {"tables": ["ORDERS"]}},
    ).json()["data"]["id"]

    differed = _run(client, src, dst)
    assert differed["result"]["summary"]["match"] is False
    incidents = client.get(f"{API}/incidents").json()["data"]
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc["kind"] == "compare"
    assert inc["status"] == "open"
    assert inc["monitor_id"] == mid
    assert inc["table_ref"] == "DB.PUBLIC.ORDERS"
    assert inc["evidence"]["compare"]["job_id"] == differed["id"]
    assert inc["evidence"]["compare"]["match"] is False
    assert "differences" in inc["summary"]

    fixed_dst = _conn(client, "dst-fixed", FULL)
    proved = _run(client, src, fixed_dst, {"incident_id": inc["id"]})
    assert proved["result"]["summary"]["match"] is True
    again = client.get(f"{API}/incidents").json()["data"]
    assert len(again) == 1
    assert again[0]["id"] == inc["id"]
    assert again[0]["status"] == "open"
    assert again[0]["evidence"]["compare"]["match"] is True
    assert again[0]["evidence"]["compare"]["job_id"] == proved["id"]
