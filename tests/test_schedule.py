"""A monitor schedule runs when due. A late measured load pages. An unmeasured one does not."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

API = "/api/v1"
TABLE = "RAW.PUBLIC.ORDERS"


def _monitor(client, schedule=None):
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "inline", "name": "c", "auth_method": "none", "coordinates": {}},
    ).json()["data"]["id"]
    body = {"connection_id": conn, "name": "orders", "scope": {"tables": ["ORDERS"]}}
    if schedule:
        body["schedule"] = schedule
    return client.post(f"{API}/monitors", json=body).json()["data"]


def test_schedule_labels_and_manual(client):
    mon = _monitor(client, "daily")
    assert mon["schedule_label"] == "Daily at 6:00 UTC"
    assert mon["schedule_cron"] == "0 6 * * *"
    assert mon["next_run"]
    assert mon["last_run_at"] is None

    updated = client.patch(f"{API}/monitors/{mon['id']}", json={"schedule": "manual"}).json()["data"]
    assert updated["schedule_label"] == "manual"
    assert updated["schedule_cron"] is None
    assert updated["next_run"] is None

    listed = client.get(f"{API}/monitors").json()["data"]
    row = next(m for m in listed if m["id"] == mon["id"])
    assert row["schedule_label"] == "manual"


def test_late_load_pages_and_a_missing_watermark_does_not(client):
    mon = _monitor(client)["id"]
    late = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    client.post(
        f"{API}/monitors/{mon}/tables/{TABLE}/snapshot",
        json={"row_count": 10, "last_load_ts": late, "columns": [{"name": "ID", "data_type": "NUMBER"}]},
    )
    incidents = client.get(f"{API}/monitors/{mon}/tables/{TABLE}/incidents").json()["data"]
    fresh = [i for i in incidents if i["kind"] == "freshness"]
    assert len(fresh) == 1
    assert fresh[0]["status"] == "open"
    assert fresh[0]["evidence"]["threshold"] == 24 * 3600

    other = _monitor(client)["id"]
    client.post(
        f"{API}/monitors/{other}/tables/{TABLE}/snapshot",
        json={
            "row_count": 10,
            "columns": [{"name": "ID", "data_type": "NUMBER"}],
            "freshness_gap": "LOAD_TS is not a column on this table",
        },
    )
    incidents = client.get(f"{API}/monitors/{other}/tables/{TABLE}/incidents").json()["data"]
    assert [i for i in incidents if i["kind"] == "freshness"] == []
