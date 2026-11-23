"""File / S3 observability — freshness from file arrival time, plus the per-monitor alert
policy (configurable warn/severe thresholds) applied identically to files and tables.

End-to-end against a real FileConnector reading a real CSV off local disk (no mocks): we set
the file's mtime to simulate when the data "arrived", profile through the HTTP API, and assert
the freshness pillar + the incident it raises honor the monitor's alert policy.
"""

from __future__ import annotations

import csv
import os
import time

from dvp.observability import alertpolicy

API = "/api/v1"


def _write_csv(path: str, rows: int = 3) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ID", "AMOUNT"])
        for i in range(rows):
            w.writerow([i, i * 10])


def _age_file(path: str, hours: float) -> None:
    """Set the file's modified time to `hours` in the past (simulates arrival time)."""
    when = time.time() - hours * 3600
    os.utime(path, (when, when))


def _file_monitor(client, path: str, policy: dict) -> str:
    conn = client.post(
        f"{API}/connections",
        json={
            "source_type": "file",
            "name": "lake",
            "auth_method": "none",
            "coordinates": {
                "database": "LAKE",
                "schema": "RAW",
                "tables": {"ORDERS": {"uri": path, "format": "csv"}},
            },
        },
    ).json()["data"]
    return client.post(
        f"{API}/monitors",
        json={
            "connection_id": conn["id"],
            "name": "orders-files",
            "scope": {"database": "LAKE", "schema": "RAW", "tables": ["ORDERS"]},
            "alert_policy": policy,
        },
    ).json()["data"]["id"]


REF = "LAKE.RAW.ORDERS"


# ----------------------------- end-to-end file freshness -----------------------------
def test_recent_file_is_fresh_no_incident(client, tmp_path):
    path = str(tmp_path / "orders.csv")
    _write_csv(path)
    _age_file(path, hours=1)  # arrived an hour ago
    policy = {"freshness": {"source": "file_arrival", "warn_after_hours": 6, "severe_after_hours": 24}}
    mid = _file_monitor(client, path, policy)

    assert client.post(f"{API}/monitors/{mid}/tables/{REF}:profile").status_code == 200

    cards = client.get(f"{API}/monitors/{mid}/tables/{REF}/pillars").json()["data"]
    by = {c["pillar"]: c for c in cards["pillars"]}
    assert by["freshness"]["configured"] is True
    assert by["freshness"]["status"] == "healthy"
    # Volume/schema still collected the same way for files.
    assert by["volume"]["headline"] == "3 rows"
    assert by["schema"]["headline"] == "2 columns"
    assert client.get(f"{API}/incidents?active_only=true").json()["data"] == []


def test_stale_file_warns_then_escalates_to_severe(client, tmp_path):
    path = str(tmp_path / "orders.csv")
    _write_csv(path)
    policy = {"freshness": {"source": "file_arrival", "warn_after_hours": 6, "severe_after_hours": 24}}
    mid = _file_monitor(client, path, policy)

    # 10h old → past warn (6h), under severe (24h) → warning.
    _age_file(path, hours=10)
    client.post(f"{API}/monitors/{mid}/tables/{REF}:profile")
    incs = client.get(f"{API}/incidents?active_only=true").json()["data"]
    fresh = [i for i in incs if i["dimension"] == "freshness"]
    assert len(fresh) == 1
    assert fresh[0]["severity"] == "warning"
    assert fresh[0]["evidence"]["source"] == "file_arrival"

    # 48h old → past severe → the same incident escalates to critical (dedup, no duplicate).
    _age_file(path, hours=48)
    client.post(f"{API}/monitors/{mid}/tables/{REF}:profile")
    incs = client.get(f"{API}/incidents?active_only=true").json()["data"]
    fresh = [i for i in incs if i["dimension"] == "freshness"]
    assert len(fresh) == 1
    assert fresh[0]["severity"] == "critical"


def test_fresh_file_auto_resolves_prior_incident(client, tmp_path):
    path = str(tmp_path / "orders.csv")
    _write_csv(path)
    policy = {"freshness": {"source": "file_arrival", "warn_after_hours": 6, "severe_after_hours": 24}}
    mid = _file_monitor(client, path, policy)

    _age_file(path, hours=30)
    client.post(f"{API}/monitors/{mid}/tables/{REF}:profile")
    assert any(
        i["dimension"] == "freshness"
        for i in client.get(f"{API}/incidents?active_only=true").json()["data"]
    )

    # A new file arrives on time → freshness incident clears.
    _age_file(path, hours=1)
    client.post(f"{API}/monitors/{mid}/tables/{REF}:profile")
    active = client.get(f"{API}/incidents?active_only=true").json()["data"]
    assert not any(i["dimension"] == "freshness" for i in active)


def test_monitor_exposes_resolved_alert_policy(client, tmp_path):
    path = str(tmp_path / "orders.csv")
    _write_csv(path)
    mid = _file_monitor(
        client, path, {"freshness": {"source": "file_arrival", "warn_after_hours": 6}}
    )
    mon = client.get(f"{API}/monitors/{mid}").json()["data"]
    # Stored keys kept, missing keys filled from defaults (so the UI always has full values).
    assert mon["alert_policy"]["freshness"]["source"] == "file_arrival"
    assert mon["alert_policy"]["freshness"]["warn_after_hours"] == 6
    assert mon["alert_policy"]["freshness"]["severe_after_hours"] == 72  # default
    assert mon["alert_policy"]["quality"]["warn_below"] == 80  # default


# ----------------------------- alert policy unit logic -----------------------------
def test_defaults_reproduce_legacy_behavior():
    # No policy → freshness warns at 24h / critical at 72h, quality <80 / <60.
    assert alertpolicy.freshness_severity(1 * 3600, None) is None
    assert alertpolicy.freshness_severity(30 * 3600, None) == "warning"
    assert alertpolicy.freshness_severity(100 * 3600, None) == "critical"
    assert alertpolicy.quality_severity(90, None) is None
    assert alertpolicy.quality_severity(75, None) == "warning"
    assert alertpolicy.quality_severity(50, None) == "critical"


def test_configured_thresholds_win():
    p = {"freshness": {"warn_after_hours": 2, "severe_after_hours": 4}}
    assert alertpolicy.freshness_severity(1 * 3600, p) is None
    assert alertpolicy.freshness_severity(3 * 3600, p) == "warning"
    assert alertpolicy.freshness_severity(5 * 3600, p) == "critical"


def test_volume_uses_pct_when_set_else_zscore():
    # Default (no pct): z-score drives severity.
    assert alertpolicy.volume_severity(None, 3.0, None) == "warning"
    assert alertpolicy.volume_severity(None, 7.0, None) == "critical"
    # Configured pct thresholds take over.
    p = {"volume": {"warn_pct": 20, "severe_pct": 50}}
    assert alertpolicy.volume_severity(25, 2.0, p) == "warning"
    assert alertpolicy.volume_severity(60, 2.0, p) == "critical"


def test_source_auto_vs_forced():
    assert alertpolicy.freshness_source(None) == "auto"
    assert alertpolicy.freshness_source({"freshness": {"source": "file_arrival"}}) == "file_arrival"
