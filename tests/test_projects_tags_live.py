"""Projects + first-class tags + project-scoped, versioned reports (real API, no mocks)."""

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


def _data_job(client, extra=None):
    s, t = _conn(client, "s"), _conn(client, "t")
    body = {
        "kind": "data", "name": "cmp",
        "source": {"connection_id": s, "table": "ORDERS"},
        "target": {"connection_id": t, "table": "ORDERS"},
        "options": {"key": ["ORDER_ID"]},
    }
    body.update(extra or {})
    job = client.post(f"{API}/compares", json=body).json()["data"]
    client.post(f"{API}/compares/{job['id']}:run")
    return job["id"]


# ----------------------------- projects -----------------------------
def test_project_crud_and_test_cases(client):
    p = client.post(f"{API}/projects", json={"name": "Migration", "tags": ["q4"]}).json()["data"]
    assert p["name"] == "Migration"
    jid = _data_job(client, {"project_id": p["id"], "source_system": "oracle", "layer": "bronze"})

    tcs = client.get(f"{API}/projects/{p['id']}/test-cases").json()["data"]
    assert [c["id"] for c in tcs["compares"]] == [jid]
    assert tcs["compares"][0]["layer"] == "bronze"

    lst = client.get(f"{API}/projects").json()["data"]
    assert next(x for x in lst if x["id"] == p["id"])["compare_count"] == 1

    # delete is a soft archive: the project is hidden but its test cases are retained
    d = client.delete(f"{API}/projects/{p['id']}")
    assert d.status_code == 200 and d.json()["data"]["archived"] is True
    # the compare is kept and still belongs to the project (never orphaned)
    assert client.get(f"{API}/compares/{jid}").json()["data"]["project_id"] == p["id"]
    # hidden from the default list, visible with include_archived, and restorable
    assert all(x["id"] != p["id"] for x in client.get(f"{API}/projects").json()["data"])
    archived = client.get(f"{API}/projects?include_archived=true").json()["data"]
    assert any(x["id"] == p["id"] for x in archived)
    r = client.post(f"{API}/projects/{p['id']}:restore")
    assert r.status_code == 200 and r.json()["data"]["archived"] is False
    assert any(x["id"] == p["id"] for x in client.get(f"{API}/projects").json()["data"])


def test_project_list_includes_health(client):
    """The project list carries per-project health (pass/diff/fail of each test case's latest run)
    so the UI can show a pass-rate ring without extra calls."""
    p = client.post(f"{API}/projects", json={"name": "HealthProj"}).json()["data"]
    # A matching compare (ORDERS vs identical ORDERS) → one passing test case.
    _data_job(client, {"project_id": p["id"]})
    h = next(x for x in client.get(f"{API}/projects").json()["data"] if x["id"] == p["id"])["health"]
    assert h["passing"] == 1 and h["differences"] == 0 and h["failed"] == 0
    assert h["pass_rate"] == 100.0


def test_compare_count_treats_a_batch_as_one_test_case(client):
    """A multi-dataset compare (a batch of N pairs) is ONE test case in the project count — not
    N — so the project header matches the Test cases tab (which counts a batch once)."""
    coords = {
        "database": "DB", "schema": "PUBLIC",
        "tables": {"ORDERS": TBL, "ITEMS": TBL, "CUSTOMERS": TBL},
    }
    src = client.post(f"{API}/connections", json={
        "source_type": "local", "name": "bsrc", "auth_method": "none", "coordinates": coords,
    }).json()["data"]["id"]
    dst = client.post(f"{API}/connections", json={
        "source_type": "local", "name": "bdst", "auth_method": "none", "coordinates": coords,
    }).json()["data"]["id"]
    p = client.post(f"{API}/projects", json={"name": "BatchProj"}).json()["data"]

    # one batch of 3 table pairs + one standalone compare = 2 test cases (not 3 + 1 = 4)
    r = client.post(f"{API}/compares/batch:submit", json={
        "kind": "data", "name": "nightly", "project_id": p["id"],
        "source": {"connection_id": src, "schema": "PUBLIC"},
        "target": {"connection_id": dst, "schema": "PUBLIC"},
        "tables": ["ORDERS", "ITEMS", "CUSTOMERS"],
        "options": {"key": ["ORDER_ID"]},
    })
    assert r.status_code == 202 and r.json()["data"]["queued"] == 3
    _data_job(client, {"project_id": p["id"]})  # one standalone compare

    lst = client.get(f"{API}/projects").json()["data"]
    assert next(x for x in lst if x["id"] == p["id"])["compare_count"] == 2


def test_project_scoped_report(client):
    p = client.post(f"{API}/projects", json={"name": "P"}).json()["data"]
    in_scope = _data_job(client, {"project_id": p["id"], "source_system": "snowflake", "layer": "gold"})
    _data_job(client)  # NOT in the project

    rep = client.post(
        f"{API}/reports",
        json={"name": "R", "report_type": "data_validation", "project_id": p["id"]},
    ).json()["data"]
    gen = client.post(f"{API}/reports/{rep['id']}:generate").json()["data"]
    assert gen["summary"]["total_test_cases"] == 1  # only the in-scope job
    doc = client.get(f"{API}/reports/{rep['id']}").json()["data"]["document"]
    assert doc["test_cases"][0]["id"] == in_scope
    assert "gold" in gen["summary"]["by_layer"]
    assert "snowflake" in gen["summary"]["by_source"]


# ----------------------------- versioning + trend -----------------------------
def test_report_versions_and_trend(client):
    _data_job(client)
    rep = client.post(f"{API}/reports", json={"name": "V", "report_type": "data_validation"}).json()["data"]
    g1 = client.post(f"{API}/reports/{rep['id']}:generate").json()["data"]
    assert g1["latest_version"] == 1
    g2 = client.post(f"{API}/reports/{rep['id']}:generate").json()["data"]
    assert g2["latest_version"] == 2

    versions = client.get(f"{API}/reports/{rep['id']}/versions").json()["data"]
    assert [v["version"] for v in versions] == [2, 1]  # newest first, immutable snapshots
    v1 = client.get(f"{API}/reports/{rep['id']}/versions/1").json()["data"]
    assert v1["document"]["version"] == 1

    trend = client.get(f"{API}/reports/{rep['id']}/trend").json()["data"]
    assert len(trend["points"]) == 2 and trend["delta"] == 0.0


# ----------------------------- first-class tags -----------------------------
def test_tag_crud_usage_and_propagation(client):
    tag = client.post(f"{API}/tags", json={"name": "critical", "color": "#ef4444"}).json()["data"]
    # duplicate rejected
    assert client.post(f"{API}/tags", json={"name": "critical"}).status_code >= 400

    jid = _data_job(client, {"tags": ["critical"]})
    tags = client.get(f"{API}/tags").json()["data"]
    assert next(t for t in tags if t["id"] == tag["id"])["usage"] == 1

    # rename propagates to the test case
    client.patch(f"{API}/tags/{tag['id']}", json={"name": "p0"})
    assert client.get(f"{API}/compares/{jid}").json()["data"]["tags"] == ["p0"]

    # delete removes it from the test case
    assert client.delete(f"{API}/tags/{tag['id']}").status_code == 204
    assert client.get(f"{API}/compares/{jid}").json()["data"]["tags"] == []


def test_assign_endpoint_sets_grouping(client):
    jid = _data_job(client)
    client.patch(
        f"{API}/compares/{jid}/assignment",
        json={"source_system": "oracle", "layer": "silver", "tags": ["pii"]},
    )
    job = client.get(f"{API}/compares/{jid}").json()["data"]
    assert job["source_system"] == "oracle" and job["layer"] == "silver" and job["tags"] == ["pii"]
