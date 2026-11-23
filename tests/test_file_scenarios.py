"""Full file-scenario coverage for data observability — every format and layout the
FileConnector supports, exercised end-to-end (no mocks) against real files on disk.

The fixtures are produced by scripts/gen_file_scenarios.py (the same tree seeded to
s3://dataq-testing-data/observability/), so these tests validate exactly what the S3
monitors see. Formats: csv, tsv, ndjson, json (array), json (records), parquet. Layouts:
single file, folder of part-files (unioned), scan-prefix discovery. Plus marker/hidden
skipping, type inference, null-rate quality, and file-arrival freshness.
"""

from __future__ import annotations

import importlib.util
import os
import time

import pytest

from dvp.connectors.base import TableRef
from dvp.connectors.file_source import FileConnector

API = "/api/v1"
_HAS_PYARROW = importlib.util.find_spec("pyarrow") is not None


def _load_generator():
    here = os.path.dirname(__file__)
    path = os.path.join(here, "..", "scripts", "gen_file_scenarios.py")
    spec = importlib.util.spec_from_file_location("gen_file_scenarios", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def scenarios(tmp_path_factory):
    """Generate the full scenario tree once per test session."""
    root = str(tmp_path_factory.mktemp("file_scenarios"))
    _load_generator().generate(root, clean=True)
    return root


def _ref(table: str) -> TableRef:
    return TableRef(database="", schema="", table=table)


# ----------------------------- connector-level: formats & layouts -----------------------------
SINGLE = [
    ("single/orders.csv", "csv", "ORDERS", 25, ["ORDER_ID", "CUSTOMER_ID", "AMOUNT", "STATUS", "ORDER_TS"]),
    ("single/customers.tsv", "tsv", "CUSTOMERS", 20, ["CUSTOMER_ID", "NAME", "EMAIL", "COUNTRY", "SIGNED_UP"]),
    ("single/events.ndjson", "ndjson", "EVENTS", 30, ["EVENT_ID", "USER_ID", "EVENT_TYPE", "EVENT_TS", "VALUE"]),
    ("single/products.json", "json", "PRODUCTS", 15, ["PRODUCT_ID", "NAME", "PRICE", "IN_STOCK"]),
    ("single/inventory.json", "json", "INVENTORY", 12, ["SKU", "WAREHOUSE", "QTY"]),
]


@pytest.mark.parametrize("rel,fmt,table,rows,cols", SINGLE)
def test_single_file_formats(scenarios, rel, fmt, table, rows, cols):
    conn = FileConnector({"uri": os.path.join(scenarios, rel), "format": fmt, "table": table})
    assert conn.list_tables() == [table]
    assert conn.count(_ref(table)) == rows
    got = {c.name for c in conn.get_schema(_ref(table))}
    assert got == set(cols)


@pytest.mark.skipif(not _HAS_PYARROW, reason="pyarrow not installed")
def test_parquet_single_file(scenarios):
    conn = FileConnector({"uri": os.path.join(scenarios, "parquet/sales.parquet"), "table": "SALES"})
    assert conn.count(_ref("SALES")) == 20
    assert {c.name for c in conn.get_schema(_ref("SALES"))} == {
        "SALE_ID", "REGION", "AMOUNT", "SALE_TS"
    }


def test_scan_prefix_discovers_files_and_folders(scenarios):
    """A prefix scan turns each file + each sub-folder one level below into a table."""
    conn = FileConnector({"uri": os.path.join(scenarios, "lake"), "scan": True})
    tables = set(conn.list_tables())
    assert tables == {"REGIONS", "SHIPMENTS", "PAYMENTS"}  # no _SUCCESS, no hidden table
    assert conn.count(_ref("REGIONS")) == 3


def test_folder_parts_are_unioned(scenarios):
    """SHIPMENTS is a folder of two 8-row part files → 16 rows; the _SUCCESS marker is ignored."""
    conn = FileConnector({"uri": os.path.join(scenarios, "lake"), "scan": True})
    assert conn.count(_ref("SHIPMENTS")) == 16


def test_hidden_and_marker_files_excluded(scenarios):
    """PAYMENTS unions two 7-row NDJSON parts (14); the .hidden.csv row is not counted."""
    conn = FileConnector({"uri": os.path.join(scenarios, "lake"), "scan": True})
    assert conn.count(_ref("PAYMENTS")) == 14


def test_type_inference_across_types(scenarios):
    conn = FileConnector(
        {"uri": os.path.join(scenarios, "typed/types_sample.csv"), "table": "TYPES"}
    )
    types = {c.name: c.data_type for c in conn.get_schema(_ref("TYPES"))}
    assert types["N_INT"] == "NUMBER"
    assert types["F_FLOAT"] == "NUMBER"
    assert types["D_DATE"] == "DATE"
    assert types["T_TIMESTAMP"] == "TIMESTAMP"
    assert types["S_STRING"] == "VARCHAR"
    # CSV has no native boolean — "True"/"False" are text, so the column infers as VARCHAR.
    assert types["B_BOOL"] == "VARCHAR"


def test_boolean_inferred_from_json(scenarios):
    """JSON (and Parquet) carry real booleans, so IN_STOCK infers as BOOLEAN."""
    conn = FileConnector(
        {"uri": os.path.join(scenarios, "single/products.json"), "format": "json", "table": "PRODUCTS"}
    )
    types = {c.name: c.data_type for c in conn.get_schema(_ref("PRODUCTS"))}
    assert types["IN_STOCK"] == "BOOLEAN"


def test_file_arrival_reads_newest_mtime(scenarios):
    """latest_file_arrival returns the newest part's mtime across a unioned folder."""
    conn = FileConnector({"uri": os.path.join(scenarios, "lake"), "scan": True})
    older, newest = time.time() - 7200, time.time() - 3600
    os.utime(os.path.join(scenarios, "lake/shipments/part-0000.csv"), (older, older))
    os.utime(os.path.join(scenarios, "lake/shipments/part-0001.csv"), (newest, newest))
    got = conn.latest_file_arrival(_ref("SHIPMENTS"))
    assert got is not None
    assert abs(got.timestamp() - newest) < 60  # newest of the two parts


# ----------------------------- end-to-end through the observability API -----------------------------
def _single_conn(client, scenarios, rel, fmt, table) -> str:
    coords = {
        "database": "FILES",
        "schema": "LAKE",
        "tables": {table: {"uri": os.path.join(scenarios, rel), "format": fmt}},
    }
    return client.post(
        f"{API}/connections",
        json={"source_type": "file", "name": f"f-{table}", "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]


def _monitor(client, conn_id, table, policy=None) -> str:
    body = {
        "connection_id": conn_id,
        "name": f"m-{table}",
        "scope": {"database": "FILES", "schema": "LAKE", "tables": [table]},
    }
    if policy is not None:
        body["alert_policy"] = policy
    return client.post(f"{API}/monitors", json=body).json()["data"]["id"]


def test_e2e_profile_single_file_pillars(client, scenarios):
    cid = _single_conn(client, scenarios, "single/orders.csv", "csv", "ORDERS")
    mid = _monitor(client, cid, "ORDERS")
    ref = "FILES.LAKE.ORDERS"
    assert client.post(f"{API}/monitors/{mid}/tables/{ref}:profile").status_code == 200
    cards = client.get(f"{API}/monitors/{mid}/tables/{ref}/pillars").json()["data"]
    by = {c["pillar"]: c for c in cards["pillars"]}
    assert by["volume"]["headline"] == "25 rows"
    assert by["schema"]["headline"] == "5 columns"
    # Freshness auto-resolves to file arrival for a file source (no watermark set).
    assert by["freshness"]["configured"] is True
    assert by["freshness"]["status"] == "healthy"  # just generated → fresh
    assert by["quality"]["headline"].endswith("/100")


def test_e2e_null_rate_drives_quality(client, scenarios):
    cid = _single_conn(client, scenarios, "typed/with_nulls.csv", "csv", "WITH_NULLS")
    mid = _monitor(client, cid, "WITH_NULLS")
    ref = "FILES.LAKE.WITH_NULLS"
    client.post(f"{API}/monitors/{mid}/tables/{ref}:profile")
    cards = client.get(f"{API}/monitors/{mid}/tables/{ref}/pillars").json()["data"]
    quality = next(c for c in cards["pillars"] if c["pillar"] == "quality")
    # VAL is null on half the rows → avg null rate 0.25 → score 75/100.
    assert quality["value"] == 75.0
    # Default alert policy warns below 80 (severe below 60) → a warning quality incident.
    incs = client.get(f"{API}/incidents?active_only=true").json()["data"]
    q = [i for i in incs if i["dimension"] == "quality" and i["monitor_id"] == mid]
    assert len(q) == 1
    assert q[0]["severity"] == "warning"


def test_e2e_scan_prefix_monitor_over_all_tables(client, scenarios):
    coords = {"uri": os.path.join(scenarios, "lake"), "scan": True}
    cid = client.post(
        f"{API}/connections",
        json={"source_type": "file", "name": "lake-scan", "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]
    mid = client.post(
        f"{API}/monitors",
        json={
            "connection_id": cid,
            "name": "lake",
            "scope": {"tables": ["REGIONS", "SHIPMENTS", "PAYMENTS"]},
            "alert_policy": {"freshness": {"source": "file_arrival"}},
        },
    ).json()["data"]["id"]
    for table, rows in [("REGIONS", "3 rows"), ("SHIPMENTS", "16 rows"), ("PAYMENTS", "14 rows")]:
        assert client.post(f"{API}/monitors/{mid}/tables/{table}:profile").status_code == 200
        cards = client.get(f"{API}/monitors/{mid}/tables/{table}/pillars").json()["data"]
        vol = next(c for c in cards["pillars"] if c["pillar"] == "volume")
        assert vol["headline"] == rows


def test_e2e_stale_file_raises_severe_incident(client, scenarios):
    # Copy orders into an isolated path we can age without disturbing other tests.
    import shutil

    aged = os.path.join(scenarios, "aged_orders.csv")
    shutil.copy(os.path.join(scenarios, "single/orders.csv"), aged)
    coords = {"database": "FILES", "schema": "LAKE", "tables": {"AGED": {"uri": aged, "format": "csv"}}}
    cid = client.post(
        f"{API}/connections",
        json={"source_type": "file", "name": "f-aged", "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]
    mid = _monitor(
        client,
        cid,
        "AGED",
        {"freshness": {"source": "file_arrival", "warn_after_hours": 6, "severe_after_hours": 24}},
    )
    old = time.time() - 48 * 3600
    os.utime(aged, (old, old))
    client.post(f"{API}/monitors/{mid}/tables/FILES.LAKE.AGED:profile")
    incs = client.get(f"{API}/incidents?active_only=true").json()["data"]
    fresh = [i for i in incs if i["dimension"] == "freshness" and i["monitor_id"] == mid]
    assert len(fresh) == 1
    assert fresh[0]["severity"] == "critical"
    assert fresh[0]["evidence"]["source"] == "file_arrival"
