"""Rename, describe, and remove a monitor, with an append-only audit trail."""

from __future__ import annotations

API = "/api/v1"


def _monitor(client) -> str:
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "snowflake", "name": "sf", "coordinates": {}, "auth_method": "key_pair"},
    ).json()["data"]
    mon = client.post(
        f"{API}/monitors",
        json={
            "connection_id": conn["id"],
            "name": "Orders",
            "description": "Nightly orders",
            "scope": {"database": "ANALYTICS", "schema": "PUBLIC"},
        },
    ).json()["data"]
    return mon["id"]


def _audit(client, monitor_id: str) -> list[dict]:
    r = client.get(f"{API}/audit", params={"entity_type": "monitor", "entity_id": monitor_id})
    assert r.status_code == 200
    return r.json()["data"]


def test_create_records_who_and_what(client):
    mid = _monitor(client)
    trail = _audit(client, mid)
    assert len(trail) == 1
    assert trail[0]["action"] == "monitor.create"
    assert trail[0]["detail"]["name"] == "Orders"
    assert trail[0]["detail"]["description"] == "Nightly orders"
    assert trail[0]["actor_email"]


def test_rename_and_description_record_before_and_after(client):
    mid = _monitor(client)
    patched = client.patch(
        f"{API}/monitors/{mid}",
        json={"name": "  Orders gold  ", "description": "Finance uses this every morning"},
    )
    assert patched.status_code == 200
    body = patched.json()["data"]
    assert body["name"] == "Orders gold"
    assert body["description"] == "Finance uses this every morning"

    # a no-op edit does not add a second audit row
    again = client.patch(f"{API}/monitors/{mid}", json={"name": "Orders gold"})
    assert again.status_code == 200

    blank = client.patch(f"{API}/monitors/{mid}", json={"name": "   "})
    assert blank.status_code == 422

    trail = _audit(client, mid)
    assert [row["action"] for row in trail] == ["monitor.update", "monitor.create"]
    changes = trail[0]["detail"]["changes"]
    assert changes["name"] == {"from": "Orders", "to": "Orders gold"}
    assert changes["description"]["from"] == "Nightly orders"
    assert changes["description"]["to"] == "Finance uses this every morning"


def test_remove_hides_the_monitor_and_keeps_the_record(client):
    mid = _monitor(client)
    listed = client.get(f"{API}/monitors").json()["data"]
    assert any(m["id"] == mid for m in listed)

    removed = client.delete(f"{API}/monitors/{mid}")
    assert removed.status_code == 200
    assert removed.json()["data"]["status"] == "deleted"

    assert client.get(f"{API}/monitors/{mid}").status_code == 404
    assert all(m["id"] != mid for m in client.get(f"{API}/monitors").json()["data"])
    assert client.delete(f"{API}/monitors/{mid}").status_code == 404

    trail = _audit(client, mid)
    assert trail[0]["action"] == "monitor.delete"
    assert trail[0]["detail"]["name"] == "Orders"
    assert trail[0]["detail"]["scope"]["schema"] == "PUBLIC"
    assert trail[0]["actor_email"]
    # the create row is still there after the monitor leaves the list
    assert any(row["action"] == "monitor.create" for row in trail)
