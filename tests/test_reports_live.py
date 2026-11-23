"""Live reports over the real API (no mocks): run compares → build/generate/export reports."""

from __future__ import annotations

API = "/api/v1"


def _cols(*names):
    return [{"name": n, "data_type": "NUMBER" if n != "STATUS" else "VARCHAR"} for n in names]


GOOD = {
    "columns": _cols("ORDER_ID", "AMOUNT", "STATUS"),
    "rows": [{"ORDER_ID": i, "AMOUNT": i * 10, "STATUS": "OPEN"} for i in (1, 2, 3)],
}
# same keys, one AMOUNT differs, one row missing, one extra → a failing data compare
BAD = {
    "columns": _cols("ORDER_ID", "AMOUNT", "STATUS"),
    "rows": [
        {"ORDER_ID": 1, "AMOUNT": 10, "STATUS": "OPEN"},
        {"ORDER_ID": 2, "AMOUNT": 999, "STATUS": "OPEN"},
        {"ORDER_ID": 4, "AMOUNT": 40, "STATUS": "NEW"},
    ],
}


def _conn(client, name, table, spec):
    coords = {"database": "DB", "schema": "PUBLIC", "tables": {table: spec}}
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": name, "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]


def _run_data_compare(client, scid, dcid):
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "data",
            "name": "cmp",
            "source": {"connection_id": scid, "table": "ORDERS"},
            "target": {"connection_id": dcid, "table": "ORDERS"},
            "options": {"key": ["ORDER_ID"]},
        },
    ).json()["data"]
    return client.post(f"{API}/compares/{job['id']}:run").json()["data"]


def _setup_two_compares(client):
    src = _conn(client, "src", "ORDERS", GOOD)
    good = _conn(client, "good", "ORDERS", GOOD)
    bad = _conn(client, "bad", "ORDERS", BAD)
    _run_data_compare(client, src, good)  # passes
    _run_data_compare(client, src, bad)  # fails


def test_data_validation_report_end_to_end(client):
    _setup_two_compares(client)
    rep = client.post(
        f"{API}/reports", json={"name": "Nightly recon", "report_type": "data_validation"}
    ).json()["data"]
    assert rep["status"] == "draft"

    gen = client.post(f"{API}/reports/{rep['id']}:generate").json()["data"]
    assert gen["status"] == "ready"
    s = gen["summary"]
    assert s["total_test_cases"] == 2
    assert s["passed"] == 1 and s["failed"] == 1
    assert s["pass_rate_pct"] == 50.0
    assert s["by_kind"]["data"] == {"total": 2, "passed": 1}

    full = client.get(f"{API}/reports/{rep['id']}").json()["data"]
    assert full["document"]["test_cases"][0]["status"] in ("pass", "fail")
    assert len(full["document"]["diagnosis"]) == 1

    # exports
    for fmt, ctype in [("json", "application/json"), ("csv", "text/csv"), ("html", "text/html")]:
        r = client.get(f"{API}/reports/{rep['id']}/export?fmt={fmt}")
        assert r.status_code == 200
        assert ctype in r.headers["content-type"]
        assert len(r.text) > 0


def test_executive_report(client):
    _setup_two_compares(client)
    rep = client.post(
        f"{API}/reports", json={"name": "Exec", "report_type": "executive"}
    ).json()["data"]
    gen = client.post(f"{API}/reports/{rep['id']}:generate").json()["data"]
    s = gen["summary"]
    assert s["validation_pass_rate_pct"] == 50.0
    assert s["open_incidents"] == 0
    assert s["health_score_pct"] is not None


def test_report_lifecycle_and_unknown_type(client):
    # listing + delete
    rep = client.post(
        f"{API}/reports", json={"name": "L", "report_type": "data_validation"}
    ).json()["data"]
    assert any(r["id"] == rep["id"] for r in client.get(f"{API}/reports").json()["data"])
    assert client.delete(f"{API}/reports/{rep['id']}").status_code == 204
    assert client.get(f"{API}/reports/{rep['id']}").status_code == 404

    # unknown report type is rejected
    bad = client.post(f"{API}/reports", json={"name": "x", "report_type": "nope"})
    assert bad.status_code == 422 or bad.json().get("status") == 422


def test_data_quality_report_many_rules_in_one_test_case(client):
    # one data-quality "test case" (monitor) bundling several rules
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "inline", "name": "raw", "auth_method": "none", "coordinates": {}},
    ).json()["data"]["id"]
    mid = client.post(
        f"{API}/monitors",
        json={"connection_id": conn, "name": "orders checks", "scope": {"tables": ["RAW_ORDERS"]}},
    ).json()["data"]["id"]
    table = "RAW.PUBLIC.RAW_ORDERS"
    specs = [
        ("no_negatives", "quality", 0, True),
        ("not_stale", "freshness", 48, False),
        ("row_volume", "volume", 0, True),
    ]
    for name, dim, val, _ in specs:
        rid = client.post(
            f"{API}/monitors/{mid}/rules",
            json={"name": name, "type": "custom_sql", "table_ref": table,
                  "config": {"expression": "SELECT 1 AS v", "threshold": 0, "dimension": dim}},
        ).json()["data"]["id"]
        client.post(
            f"{API}/monitors/{mid}/rules/{rid}:record",
            json={"table_ref": table, "metric_value": val, "ts": "2026-10-08T00:00:00Z"},
        )

    rep = client.post(
        f"{API}/reports",
        json={"name": "DQ", "report_type": "data_quality", "spec": {"monitor_ids": [mid]}},
    ).json()["data"]
    gen = client.post(f"{API}/reports/{rep['id']}:generate").json()["data"]
    assert gen["summary"]["total_rules"] == 3
    assert set(gen["summary"]["by_dimension"]) == {"quality", "freshness", "volume"}
    doc = client.get(f"{API}/reports/{rep['id']}").json()["data"]["document"]
    assert any(r["name"] == "not_stale" for r in doc["failing_rules"])


def test_export_before_generate_fails(client):
    rep = client.post(
        f"{API}/reports", json={"name": "ng", "report_type": "data_validation"}
    ).json()["data"]
    r = client.get(f"{API}/reports/{rep['id']}/export?fmt=json")
    assert r.status_code >= 400
