"""Monitor analysis window (lookback_days) — defaults to 30, settable, drives the history window."""

from __future__ import annotations

API = "/api/v1"


def _conn(client) -> str:
    return client.post(f"{API}/connections", json={
        "source_type": "local", "name": "c", "auth_method": "none",
        "coordinates": {"schema": "PUBLIC", "tables": {}}}).json()["data"]["id"]


def test_create_defaults_lookback_to_30(client):
    cid = _conn(client)
    mon = client.post(f"{API}/monitors", json={
        "connection_id": cid, "name": "m", "scope": {"schema": "PUBLIC", "tables": ["T"]}}).json()["data"]
    assert mon["lookback_days"] == 30


def test_create_accepts_custom_lookback(client):
    cid = _conn(client)
    mon = client.post(f"{API}/monitors", json={
        "connection_id": cid, "name": "m2", "scope": {"schema": "PUBLIC", "tables": ["T"]},
        "lookback_days": 90}).json()["data"]
    assert mon["lookback_days"] == 90
    assert client.get(f"{API}/monitors/{mon['id']}").json()["data"]["lookback_days"] == 90


def test_update_lookback(client):
    cid = _conn(client)
    mid = client.post(f"{API}/monitors", json={
        "connection_id": cid, "name": "m3", "scope": {"schema": "PUBLIC", "tables": ["T"]}}).json()["data"]["id"]
    r = client.patch(f"{API}/monitors/{mid}", json={"lookback_days": 7})
    assert r.status_code == 200
    assert r.json()["data"]["lookback_days"] == 7


def test_lookback_bounds_rejected(client):
    cid = _conn(client)
    r = client.post(f"{API}/monitors", json={
        "connection_id": cid, "name": "bad", "scope": {"tables": ["T"]}, "lookback_days": 0})
    assert r.status_code >= 400  # ge=1
