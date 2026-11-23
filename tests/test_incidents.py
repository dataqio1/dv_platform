"""Anomaly detection + incident lifecycle (dedup, auto-resolve, evidence) — no mocks."""

from __future__ import annotations

from dvp.observability.anomaly import detect_anomaly

API = "/api/v1"
TABLE = "ANALYTICS.PUBLIC.ORDERS"


# ----------------------------- pure anomaly -----------------------------
def test_anomaly_detects_drop():
    series = [1000, 1010, 990, 1005, 995, 1000, 1002, 300]
    res = detect_anomaly(series, direction="both")
    assert res.anomaly is True
    assert res.direction == "drop"
    assert res.lo is not None and res.value == 300


def test_anomaly_thin_history_no_alarm():
    res = detect_anomaly([100, 50, 200], direction="both")  # too few points
    assert res.anomaly is False
    assert res.reason == "insufficient history"


def test_anomaly_within_band():
    series = [1000, 1010, 990, 1005, 995, 1000, 1002, 998]
    assert detect_anomaly(series).anomaly is False


# ----------------------------- end-to-end incidents -----------------------------
def _monitor(client) -> str:
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "inline", "name": "c", "auth_method": "none", "coordinates": {}},
    ).json()["data"]
    return client.post(
        f"{API}/monitors",
        json={"connection_id": conn["id"], "name": "m", "scope": {"tables": ["ORDERS"]}},
    ).json()["data"]["id"]


def _snap(client, mid, rows, ts):
    client.post(
        f"{API}/monitors/{mid}/tables/{TABLE}/snapshot",
        json={"row_count": rows, "ts": ts},
    )


def _mondays(n: int) -> list[str]:
    from datetime import UTC, datetime, timedelta

    day = datetime(2026, 9, 1, tzinfo=UTC)
    found = []
    while len(found) < n:
        if day.weekday() == 0:
            found.append(day.strftime("%Y-%m-%dT06:00:00Z"))
        day += timedelta(days=1)
    return found


def test_weekday_volume_ignores_a_normal_monday_and_pages_a_real_drop(client):
    mid = _monitor(client)
    mondays = _mondays(6)
    # Three Mondays of ~400, plus fat Thursdays that must not set the Monday band.
    for ts, rows in (
        (mondays[0], 400),
        ("2026-09-03T06:00:00Z", 5000),  # Thursday
        (mondays[1], 410),
        ("2026-09-10T06:00:00Z", 5200),
        (mondays[2], 390),
    ):
        _snap(client, mid, rows, ts)
    pillars = client.get(f"{API}/monitors/{mid}/tables/{TABLE}/pillars").json()["data"]
    volume = next(c for c in pillars["pillars"] if c["pillar"] == "volume")
    assert volume["detail"] == "need 1 more Monday scan before volume can alert"
    assert client.get(f"{API}/monitors/{mid}/tables/{TABLE}/incidents").json()["data"] == []

    _snap(client, mid, 405, mondays[3])  # a normal Monday, now with three prior Mondays
    assert client.get(f"{API}/monitors/{mid}/tables/{TABLE}/incidents").json()["data"] == []
    pillars = client.get(f"{API}/monitors/{mid}/tables/{TABLE}/pillars").json()["data"]
    volume = next(c for c in pillars["pillars"] if c["pillar"] == "volume")
    assert volume["detail"] == "within Monday band"
    assert volume["status"] == "healthy"

    _snap(client, mid, 40, mondays[4])  # this Monday is not like the others
    inc = client.get(f"{API}/monitors/{mid}/tables/{TABLE}/incidents").json()["data"]
    assert len(inc) == 1
    assert inc[0]["kind"] == "anomaly"
    assert inc[0]["status"] == "open"
    assert inc[0]["evidence"]["direction"] == "drop"
    assert inc[0]["evidence"]["value"] == 40
    hist = client.get(
        f"{API}/monitors/{mid}/tables/{TABLE}/pillars/volume/history"
    ).json()["data"]["points"]
    red = [p for p in hist if p["anomaly"]]
    assert red
    assert red[-1]["value"] == inc[0]["evidence"]["value"]
    assert red[-1]["lo"] == inc[0]["evidence"]["lo"]

    _snap(client, mid, 400, mondays[5])  # recovers
    inc = client.get(f"{API}/monitors/{mid}/tables/{TABLE}/incidents").json()["data"]
    assert inc[0]["status"] == "resolved"


def test_rule_failure_incident_dedups_and_resolves(client):
    mid = _monitor(client)
    rid = client.post(
        f"{API}/monitors/{mid}/rules",
        json={
            "name": "neg",
            "type": "custom_sql",
            "table_ref": TABLE,
            "severity": "critical",
            "config": {"threshold": 0},
        },
    ).json()["data"]["id"]

    # two failing evaluations → ONE incident, occurrences=2 (dedup, not spam)
    for ts in ("2026-10-01", "2026-10-02"):
        client.post(
            f"{API}/monitors/{mid}/rules/{rid}:record",
            json={"table_ref": TABLE, "metric_value": 5, "ts": f"{ts}T00:00:00Z"},
        )
    active = client.get(f"{API}/monitors/{mid}/incidents?active_only=true").json()["data"]
    assert len(active) == 1
    assert active[0]["occurrences"] == 2
    assert active[0]["kind"] == "rule"
    assert active[0]["severity"] == "critical"

    # a passing evaluation resolves it
    client.post(
        f"{API}/monitors/{mid}/rules/{rid}:record",
        json={"table_ref": TABLE, "metric_value": 0, "ts": "2026-10-03T00:00:00Z"},
    )
    assert client.get(f"{API}/monitors/{mid}/incidents?active_only=true").json()["data"] == []


def test_incident_ack_and_resolve_endpoints(client):
    mid = _monitor(client)
    rid = client.post(
        f"{API}/monitors/{mid}/rules",
        json={"name": "neg", "type": "custom_sql", "table_ref": TABLE, "config": {"threshold": 0}},
    ).json()["data"]["id"]
    client.post(
        f"{API}/monitors/{mid}/rules/{rid}:record", json={"table_ref": TABLE, "metric_value": 3}
    )
    inc_id = client.get(f"{API}/monitors/{mid}/incidents").json()["data"][0]["id"]

    acked = client.post(f"{API}/incidents/{inc_id}:ack").json()["data"]
    assert acked["status"] == "acknowledged"
    resolved = client.post(f"{API}/incidents/{inc_id}:resolve").json()["data"]
    assert resolved["status"] == "resolved"
    assert resolved["resolved_at"] is not None
