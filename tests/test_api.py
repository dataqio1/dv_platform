"""API-level tests via TestClient against the in-memory local stack (no mocks)."""

from __future__ import annotations

API = "/api/v1"


def test_health_and_whoami(client):
    r = client.get(f"{API}/health")
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["status"] == "ok"
    assert body["data"]["profile"] == "local"

    r = client.get(f"{API}/whoami")
    assert r.status_code == 200
    who = r.json()["data"]
    assert who["group_id"]
    assert who["role"] == "group_admin"


def test_connection_crud_and_secret(client):
    r = client.post(
        f"{API}/connections",
        json={
            "source_type": "snowflake",
            "name": "prod-sf",
            "coordinates": {"account": "abc123"},
            "auth_method": "key_pair",
            "secret": {"private_key": "-----BEGIN..."},
        },
    )
    assert r.status_code == 201
    conn = r.json()["data"]
    assert conn["has_secret"] is True
    # secret material is never returned
    assert "private_key" not in r.text

    cid = conn["id"]
    assert client.get(f"{API}/connections/{cid}").json()["data"]["name"] == "prod-sf"
    assert len(client.get(f"{API}/connections").json()["data"]) == 1
    assert client.delete(f"{API}/connections/{cid}").status_code == 200


def test_monitor_and_rule_lifecycle(client):
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "snowflake", "name": "sf", "coordinates": {}, "auth_method": "key_pair"},
    ).json()["data"]

    mon = client.post(
        f"{API}/monitors",
        json={"connection_id": conn["id"], "name": "orders", "scope": {"schema": "PUBLIC"}},
    ).json()["data"]
    mid = mon["id"]

    # create rule
    rule = client.post(
        f"{API}/monitors/{mid}/rules",
        json={"name": "nn-email", "type": "not_null", "columns": ["EMAIL"]},
    ).json()["data"]
    assert rule["version"] == 1

    # update bumps version (runtime-edit safety)
    patched = client.patch(
        f"{API}/monitors/{mid}/rules/{rule['id']}",
        json={"severity": "critical"},
    ).json()["data"]
    assert patched["version"] == 2
    assert patched["severity"] == "critical"

    # soft delete -> disappears from list
    client.delete(f"{API}/monitors/{mid}/rules/{rule['id']}")
    assert client.get(f"{API}/monitors/{mid}/rules").json()["data"] == []


def test_autogenerate_levels(client):
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "snowflake", "name": "sf", "coordinates": {}, "auth_method": "key_pair"},
    ).json()["data"]
    mon = client.post(
        f"{API}/monitors",
        json={"connection_id": conn["id"], "name": "m", "scope": {}},
    ).json()["data"]

    r = client.post(f"{API}/monitors/{mon['id']}/rules:autogenerate", json={"level": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["level"] == 3
    assert body["meta"]["count"] > 0
    # all generated rules are suggested
    rules = client.get(f"{API}/monitors/{mon['id']}/rules").json()["data"]
    assert all(rr["state"] == "suggested" for rr in rules)


def test_not_found_problem_json_shape(client):
    # Unknown monitor id -> typed problem+json (docs/design/08 §1).
    r = client.get(f"{API}/monitors/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert body["title"] == "not_found"
    assert body["status"] == 404
    assert "detail" in body
