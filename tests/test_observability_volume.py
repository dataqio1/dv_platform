"""Load type (truncate/incremental), volume-anomaly band in history, and timeline window."""

from __future__ import annotations

API = "/api/v1"
TABLE = "RAW.PUBLIC.ORDERS"


def _monitor(client, load_type: str | None = None) -> str:
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "inline", "name": "raw", "auth_method": "none", "coordinates": {}},
    ).json()["data"]
    body = {"connection_id": conn["id"], "name": "m", "scope": {"tables": ["ORDERS"]}}
    if load_type:
        body["load_type"] = load_type
    return client.post(f"{API}/monitors", json=body).json()["data"]["id"]


def test_load_type_default_and_set(client):
    mid = _monitor(client)
    assert client.get(f"{API}/monitors/{mid}").json()["data"]["load_type"] == "incremental"
    # change to truncate-and-load
    r = client.post(f"{API}/monitors/{mid}:load-type", json={"load_type": "full_reload"})
    assert r.status_code == 200 and r.json()["data"]["load_type"] == "full_reload"
    # unknown rejected
    assert client.post(f"{API}/monitors/{mid}:load-type", json={"load_type": "bogus"}).status_code >= 400


def test_load_type_at_create_and_in_table_list(client):
    mid = _monitor(client, load_type="full_reload")
    client.post(f"{API}/monitors/{mid}/tables/{TABLE}/snapshot", json={"row_count": 100})
    tables = client.get(f"{API}/monitors/{mid}/tables").json()["data"]
    assert tables[0]["load_type"] == "full_reload"


def test_one_friday_says_how_many_more_scans_volume_needs(client):
    mid = _monitor(client)
    client.post(
        f"{API}/monitors/{mid}/tables/{TABLE}/snapshot",
        json={"row_count": 19879, "ts": "2026-10-16T21:00:00Z"},
    )
    pillars = client.get(f"{API}/monitors/{mid}/tables/{TABLE}/pillars").json()["data"]
    volume = next(c for c in pillars["pillars"] if c["pillar"] == "volume")
    assert volume["status"] == "healthy"
    assert volume["headline"] == "19,879 rows"
    assert volume["detail"] == "need 3 more Friday scans before volume can alert"
    assert client.get(f"{API}/monitors/{mid}/tables/{TABLE}/incidents").json()["data"] == []


def test_volume_history_flags_spike_against_baseline(client):
    from datetime import UTC, datetime, timedelta

    mid = _monitor(client)
    # Same weekday, so the band is that weekday and not the mixed week.
    series = [1000, 1010, 990, 1005, 5000]
    start = datetime(2026, 9, 7, tzinfo=UTC)
    for i, rc in enumerate(series):
        ts = (start + timedelta(days=7 * i)).strftime("%Y-%m-%dT00:00:00Z")
        client.post(
            f"{API}/monitors/{mid}/tables/{TABLE}/snapshot",
            json={"row_count": rc, "ts": ts},
        )
    hist = client.get(f"{API}/monitors/{mid}/tables/{TABLE}/pillars/volume/history").json()["data"]
    pts = hist["points"]
    assert [p["value"] for p in pts] == series  # chronological
    # every point carries a baseline band
    assert all("lo" in p and "hi" in p for p in pts)
    # the final 5000 is flagged a spike; the steady points are not
    assert pts[-1]["anomaly"] is True and pts[-1]["direction"] == "spike"
    assert hist["anomaly_count"] >= 1
    assert not any(p.get("anomaly") for p in pts[:-1])
    assert hist["baseline"]["hi"] is not None  # current expected band for the overlay


def test_history_timeline_window(client):
    mid = _monitor(client)
    # 20 daily points
    for i in range(20):
        client.post(
            f"{API}/monitors/{mid}/tables/{TABLE}/snapshot",
            json={"row_count": 1000 + i, "ts": f"2026-09-{i + 1:02d}T00:00:00Z"},
        )
    full = client.get(f"{API}/monitors/{mid}/tables/{TABLE}/pillars/volume/history").json()["data"]
    assert len(full["points"]) == 20
    # a tight window returns fewer points (relative to the seeded dates)
    recent = client.get(
        f"{API}/monitors/{mid}/tables/{TABLE}/pillars/volume/history?since_days=100000"
    ).json()["data"]
    assert len(recent["points"]) == 20  # huge window → all
    narrow = client.get(
        f"{API}/monitors/{mid}/tables/{TABLE}/pillars/volume/history?since_days=1"
    ).json()["data"]
    # every seeded point is dated in September, well over a day ago → the 1-day window is empty
    assert narrow["points"] == []
