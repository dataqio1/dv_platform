"""Richer monitor schedules — set a raw cron via update, friendly labels, validation."""

from __future__ import annotations

from dvp.core.cron import is_valid
from dvp.observability.service import schedule_label

API = "/api/v1"


def test_cron_validation():
    assert is_valid("0 6 * * *")
    assert is_valid("30 2 * * 1")
    assert is_valid("0 */4 * * *")
    assert not is_valid("99 * * * *")   # minute out of range
    assert not is_valid("0 6 * *")      # too few fields
    assert not is_valid("nonsense")


def test_schedule_labels():
    assert schedule_label("0 * * * *") == "Every hour"
    assert schedule_label("0 */6 * * *") == "Every 6 hours"
    assert schedule_label("30 2 * * *") == "Daily at 2:30 UTC"
    assert schedule_label("0 9 * * 1") == "Weekly on Monday at 9:00 UTC"
    assert schedule_label("0 0 15 * *") == "Monthly on the 15th at 0:00 UTC"
    assert schedule_label(None) == "manual"


def _monitor(client) -> str:
    cid = client.post(f"{API}/connections", json={
        "source_type": "local", "name": "c", "auth_method": "none", "coordinates": {"tables": {}}}).json()["data"]["id"]
    return client.post(f"{API}/monitors", json={
        "connection_id": cid, "name": "m", "scope": {"tables": []}}).json()["data"]["id"]


def test_update_sets_custom_cron(client):
    mid = _monitor(client)
    r = client.patch(f"{API}/monitors/{mid}", json={"schedule_cron": "30 2 * * 1"})
    assert r.status_code == 200
    assert r.json()["data"]["schedule_cron"] == "30 2 * * 1"

    got = client.get(f"{API}/monitors/{mid}").json()["data"]
    assert got["schedule_cron"] == "30 2 * * 1"
    assert got["schedule_label"] == "Weekly on Monday at 2:30 UTC"


def test_update_manual_clears_schedule(client):
    mid = _monitor(client)
    client.patch(f"{API}/monitors/{mid}", json={"schedule_cron": "0 6 * * *"})
    r = client.patch(f"{API}/monitors/{mid}", json={"schedule_cron": ""})  # Manual
    assert r.json()["data"]["schedule_cron"] in (None, "")


def test_update_rejects_bad_cron(client):
    mid = _monitor(client)
    r = client.patch(f"{API}/monitors/{mid}", json={"schedule_cron": "99 99 * * *"})
    assert r.status_code >= 400
