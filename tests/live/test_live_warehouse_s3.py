"""LIVE integration tests — real Snowflake + real S3, real data, no mocks.

These exercise the data-observability UI's backend contract end-to-end over the actual HTTP
API against the *established* connections in the desktop store (`.dvp/dvp.db` + file-backed
secrets):

  * Snowflake  — key-pair connection `snowflake-dwh-test` → DWH_TEST.DMS_BRONZE (BRZ_* tables)
  * S3         — `dataq-testing-data/observability/...` seeded by scripts/seed_s3_file_scenarios.py

The app boots in local mode against a *throwaway copy* of the real DB + secrets (so tests never
mutate the dev store), and because the store's `group_admin` is `local@dv-platform.dev` the
bootstrapped local tenant reuses the group that owns these connections — so the real API sees them.

Skips cleanly (never fails) when the desktop store is absent or the backend is unreachable — e.g.
CI with no credentials — so `pytest` stays green everywhere; it runs for real wherever the
connections + network exist.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

API = "/api/v1"
REPO = Path(__file__).resolve().parents[2]  # tests/live/ → repo root
DESKTOP_DB = REPO / ".dvp" / "dvp.db"
DESKTOP_SECRETS = REPO / ".dvp" / "secrets"


# ----------------------------- fixtures / gating -----------------------------
@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """A TestClient wired to a throwaway copy of the desktop store. Skips if absent."""
    if not DESKTOP_DB.exists() or not DESKTOP_SECRETS.exists():
        pytest.skip("no desktop store (.dvp/dvp.db + secrets) — live backends unavailable")

    work = tmp_path_factory.mktemp("live_store")
    db = work / "dvp.db"
    secrets = work / "secrets"
    results = work / "results"
    shutil.copy(DESKTOP_DB, db)
    shutil.copytree(DESKTOP_SECRETS, secrets)
    results.mkdir()

    from fastapi.testclient import TestClient

    from dvp.api.app import create_app
    from dvp.config import Settings

    settings = Settings(
        profile="local",
        database_url=f"sqlite:///{db}",
        secret_backend="file",
        secret_local_dir=str(secrets),
        result_backend="local",
        result_local_dir=str(results),
    )
    with TestClient(create_app(settings)) as client:
        conns = {c["name"]: c["id"] for c in client.get(f"{API}/connections").json()["data"]}

        class Live:
            pass

        obj = Live()
        obj.client = client
        obj.conns = conns
        yield obj


def _conn_id(live, name: str) -> str:
    cid = live.conns.get(name)
    if not cid:
        pytest.skip(f"connection {name!r} not present in the desktop store")
    return cid


def _require_reachable(live, cid: str, label: str) -> None:
    r = live.client.post(f"{API}/connections/{cid}:test")
    if r.status_code != 200 or not r.json()["data"].get("ok"):
        pytest.skip(f"{label} unreachable from this environment: {r.text[:160]}")


def _pillars(live, mid: str, ref: str) -> dict:
    return {
        p["pillar"]: p
        for p in live.client.get(f"{API}/monitors/{mid}/tables/{ref}/pillars").json()["data"]["pillars"]
    }


def _rows_in(headline: str) -> int:
    m = re.search(r"([\d,]+)\s+rows", headline or "")
    return int(m.group(1).replace(",", "")) if m else -1


# ----------------------------- Snowflake (real warehouse) -----------------------------
SF_CONN = "snowflake-dwh-test"
SF_SCOPE = {"database": "DWH_TEST", "schema": "DMS_BRONZE"}
SF_ORDERS = "DWH_TEST.DMS_BRONZE.BRZ_ORDERS"


def _sf_monitor(live, **extra) -> str:
    cid = _conn_id(live, SF_CONN)
    _require_reachable(live, cid, "Snowflake")
    body = {"connection_id": cid, "name": "live-sf-orders",
            "scope": {**SF_SCOPE, "tables": ["BRZ_ORDERS"]}, **extra}
    r = live.client.post(f"{API}/monitors", json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def test_snowflake_connection_test_ok(live):
    cid = _conn_id(live, SF_CONN)
    r = live.client.post(f"{API}/connections/{cid}:test")
    if r.status_code != 200 or not r.json()["data"].get("ok"):
        pytest.skip(f"Snowflake unreachable: {r.text[:160]}")
    assert "Snowflake" in r.json()["data"]["detail"]


def test_snowflake_catalog_lists_bronze_tables(live):
    cid = _conn_id(live, SF_CONN)
    _require_reachable(live, cid, "Snowflake")
    r = live.client.get(f"{API}/connections/{cid}/tables", params={"schema": "DMS_BRONZE"})
    assert r.status_code == 200, r.text
    names = {t["name"] for t in r.json()["data"]["tables"]}
    # the seed creates one representative table per load pattern — BRZ_ORDERS always exists
    assert "BRZ_ORDERS" in names


def test_snowflake_profile_and_pillars_on_real_data(live):
    mid = _sf_monitor(live)
    prof = live.client.post(f"{API}/monitors/{mid}/tables/{SF_ORDERS}:profile")
    assert prof.status_code == 200, prof.text

    pil = _pillars(live, mid, SF_ORDERS)
    # The UI's table page renders these pillar cards; assert the real contract + real values.
    assert {"freshness", "volume", "schema", "quality"} <= set(pil)
    assert pil["volume"]["configured"] is True
    assert pil["volume"]["status"] in {"healthy", "warning", "critical"}
    assert _rows_in(pil["volume"]["headline"]) > 1000          # BRZ_ORDERS is ~19.8k real rows
    assert pil["schema"]["configured"] is True
    assert pil["schema"]["headline"].endswith("columns")
    assert pil["quality"]["configured"] is True


def test_snowflake_watermark_makes_freshness_configured(live):
    # BRZ_ORDERS carries a LOAD_TS load-time column; wiring it as the watermark turns the
    # freshness pillar from "unknown" into a real, configured signal (UI select-data step).
    mid = _sf_monitor(live, watermark_column="LOAD_TS", watermark_type="timestamp")
    assert live.client.post(f"{API}/monitors/{mid}/tables/{SF_ORDERS}:profile").status_code == 200
    fresh = _pillars(live, mid, SF_ORDERS)["freshness"]
    assert fresh["configured"] is True
    assert fresh["status"] in {"healthy", "warning", "critical"}


def test_monitor_summary_reflects_live_monitor(live):
    mid = _sf_monitor(live)
    live.client.post(f"{API}/monitors/{mid}/tables/{SF_ORDERS}:profile")
    summary = live.client.get(f"{API}/monitors:summary")
    assert summary.status_code == 200, summary.text
    data = summary.json()["data"]
    assert data["total"] >= 1
    assert set(data["health"]) == {"healthy", "warning", "error", "paused"}
    assert sum(data["health"].values()) == data["total"]


# ----------------------------- S3 (real object storage) -----------------------------
S3_CSV = "Orders file (CSV)"
S3_PARQUET = "Sales file (Parquet)"
S3_LAKE = "Data lake (scan prefix)"


def _s3_monitor(live, conn_name: str, table: str) -> str:
    cid = _conn_id(live, conn_name)
    _require_reachable(live, cid, f"S3 ({conn_name})")
    r = live.client.post(
        f"{API}/monitors",
        json={"connection_id": cid, "name": f"live-s3-{table.lower()}",
              "scope": {"database": "S3", "schema": "SINGLE", "tables": [table]}},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def test_s3_connection_test_ok(live):
    cid = _conn_id(live, S3_CSV)
    r = live.client.post(f"{API}/connections/{cid}:test")
    if r.status_code != 200 or not r.json()["data"].get("ok"):
        pytest.skip(f"S3 unreachable: {r.text[:160]}")
    assert "s3://dataq-testing-data" in r.json()["data"]["detail"]


def test_s3_csv_profile_and_pillars(live):
    mid = _s3_monitor(live, S3_CSV, "ORDERS")
    ref = "S3.SINGLE.ORDERS"
    assert live.client.post(f"{API}/monitors/{mid}/tables/{ref}:profile").status_code == 200
    pil = _pillars(live, mid, ref)
    assert {"freshness", "volume", "schema", "quality"} <= set(pil)
    # file freshness comes from the S3 object's last-modified time → a real, configured signal
    assert pil["freshness"]["configured"] is True
    assert _rows_in(pil["volume"]["headline"]) > 0
    assert pil["schema"]["headline"].endswith("columns")


def test_s3_parquet_profile(live):
    mid = _s3_monitor(live, S3_PARQUET, "SALES")
    ref = "S3.SINGLE.SALES"
    prof = live.client.post(f"{API}/monitors/{mid}/tables/{ref}:profile")
    assert prof.status_code == 200, prof.text
    assert "volume" in prof.json()["data"]["pillars_recorded"]
    pil = _pillars(live, mid, ref)
    assert _rows_in(pil["volume"]["headline"]) > 0
    assert pil["schema"]["configured"] is True


def test_s3_lake_scan_discovers_datasets(live):
    # The "select file datasets" UI step lists every dataset one level below a scan prefix.
    cid = _conn_id(live, S3_LAKE)
    _require_reachable(live, cid, "S3 (lake scan)")
    r = live.client.get(f"{API}/connections/{cid}/tables", params={"schema": "LAKE"})
    assert r.status_code == 200, r.text
    names = {t["name"] for t in r.json()["data"]["tables"]}
    assert {"REGIONS", "SHIPMENTS", "PAYMENTS"} <= names
