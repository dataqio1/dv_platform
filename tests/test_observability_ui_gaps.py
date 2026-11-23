"""UI-gap backends: table loading-behavior DETECTION (Figma screen 12) and S3 file-dataset
DISCOVERY + bulk verify (Figma screen 4).

No mocks: every assertion drives the real FastAPI app over HTTP against the real in-memory
SQLite store the app boots with. API -> service -> connector (real inline/file source) ->
DB -> response, end to end. Verify-state persistence is checked by re-querying.
"""

from __future__ import annotations

import csv

API = "/api/v1"


# ----------------------------- Feature A helpers -----------------------------
def _inline_connection(client, tables: dict) -> str:
    return client.post(
        f"{API}/connections",
        json={
            "source_type": "inline",
            "name": "warehouse",
            "auth_method": "none",
            "coordinates": {"database": "DWH", "schema": "PUBLIC", "tables": tables},
        },
    ).json()["data"]["id"]


def _monitor_over(client, conn_id: str, tables: list[str]) -> str:
    return client.post(
        f"{API}/monitors",
        json={
            "connection_id": conn_id,
            "name": "load-type-mon",
            "scope": {"database": "DWH", "schema": "PUBLIC", "tables": tables},
        },
    ).json()["data"]["id"]


# SCD-2 style table: effective_from + is_current → classifier says scd2.
_SCD2 = {
    "columns": [
        {"name": "CUSTOMER_ID", "data_type": "NUMBER", "nullable": False},
        {"name": "NAME", "data_type": "VARCHAR", "nullable": True},
        {"name": "EFFECTIVE_FROM", "data_type": "DATE", "nullable": False},
        {"name": "VALID_TO", "data_type": "DATE", "nullable": True},
        {"name": "IS_CURRENT", "data_type": "BOOLEAN", "nullable": False},
        {"name": "INGESTED_AT", "data_type": "TIMESTAMP", "nullable": False},
    ],
    "row_count": 5000,
}
# Append-only style: an ingest/load timestamp, no update column → append.
# INGESTED_AT drives the classifier's append signal; LOADED_AT is the strongest watermark name.
_APPEND = {
    "columns": [
        {"name": "EVENT_ID", "data_type": "NUMBER", "nullable": False},
        {"name": "PAYLOAD", "data_type": "VARCHAR", "nullable": True},
        {"name": "INGESTED_AT", "data_type": "TIMESTAMP", "nullable": False},
        {"name": "LOADED_AT", "data_type": "TIMESTAMP", "nullable": False},
        {"name": "BATCH_ID", "data_type": "NUMBER", "nullable": False},
    ],
    "row_count": 100000,
}


# ----------------------------- Feature A tests -----------------------------
def test_detect_load_type_classifies_scd2_and_suggests_columns(client):
    conn = _inline_connection(client, {"DIM_CUSTOMER": _SCD2})
    mid = _monitor_over(client, conn, ["DIM_CUSTOMER"])

    res = client.post(f"{API}/monitors/{mid}:detect-load-type")
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["monitor_id"] == mid
    assert data["tables_inspected"] == ["DIM_CUSTOMER"]

    result = data["results"][0]
    assert result["table_ref"] == "DIM_CUSTOMER"
    assert result["load_type"] == "scd2"
    assert 0.0 <= result["confidence"] <= 1.0 and result["confidence"] > 0
    assert result["reason"]
    # SCD-2 role columns are suggested.
    assert result["column_mappings"]["valid_from"] == "EFFECTIVE_FROM"
    assert result["column_mappings"]["is_current"] == "IS_CURRENT"
    assert result["column_mappings"]["valid_to"] == "VALID_TO"
    # column_candidates present per role (the picker's options).
    assert "EFFECTIVE_FROM" in result["column_candidates"]["valid_from"]
    assert result["column_candidates"]["business_key"]  # CUSTOMER_ID matched
    # alternatives are other plausible types, never the chosen one.
    assert result["alternatives"]
    assert all(a["load_type"] != "scd2" for a in result["alternatives"])


def test_detect_load_type_classifies_append_and_surfaces_watermark(client):
    conn = _inline_connection(client, {"EVENTS": _APPEND})
    mid = _monitor_over(client, conn, ["EVENTS"])

    data = client.post(f"{API}/monitors/{mid}:detect-load-type").json()["data"]
    result = data["results"][0]
    assert result["load_type"] == "append"
    # watermark/LOADED_AT role + the monitor-level watermark suggestion.
    assert result["column_mappings"]["watermark"] == "LOADED_AT"
    assert data["watermark"] is not None
    assert data["watermark"]["column"] == "LOADED_AT"
    # append-only datasets expose BATCH_ID + EVENT_ID roles.
    assert result["column_mappings"]["batch_id"] == "BATCH_ID"
    assert result["column_mappings"]["event_id"] == "EVENT_ID"


def test_detect_load_type_warns_on_unreadable_table(client):
    # Scope lists a table the inline source doesn't have → classified set skips it, warning raised.
    conn = _inline_connection(client, {"EVENTS": _APPEND})
    mid = _monitor_over(client, conn, ["EVENTS", "GHOST_TABLE"])

    res = client.post(f"{API}/monitors/{mid}:detect-load-type")
    assert res.status_code == 200, res.text
    body = res.json()
    assert [r["table_ref"] for r in body["data"]["results"]] == ["EVENTS"]
    assert any(w["code"] == "schema_fetch_failed" for w in body["warnings"])
    assert any("GHOST_TABLE" in (w.get("details") or {}).get("table", "") for w in body["warnings"])


# ----------------------------- Feature B helpers -----------------------------
def _write_csv(path, header, rows):  # noqa: ANN001, ANN201
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def _file_connection_scan(client, prefix: str) -> str:
    return client.post(
        f"{API}/connections",
        json={
            "source_type": "file",
            "name": "lake",
            "auth_method": "none",
            "coordinates": {"database": "LAKE", "schema": "RAW", "uri": prefix, "scan": True},
        },
    ).json()["data"]["id"]


# ----------------------------- Feature B tests -----------------------------
def test_list_file_datasets_discovers_with_status_and_pagination(client, tmp_path):
    lake = tmp_path / "lake"
    lake.mkdir()
    _write_csv(str(lake / "orders.csv"), ["ORDER_ID", "AMOUNT"], [[1, 10], [2, 20]])
    _write_csv(str(lake / "customers.csv"), ["CUSTOMER_ID", "NAME"], [[1, "a"]])
    _write_csv(str(lake / "items.csv"), ["ITEM_ID"], [[1]])
    cid = _file_connection_scan(client, str(lake))

    res = client.get(f"{API}/connections/{cid}/file-datasets")
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["total"] == 3
    names = {d["table_name"] for d in data["datasets"]}
    assert names == {"ORDERS", "CUSTOMERS", "ITEMS"}
    for d in data["datasets"]:
        assert d["verification_status"] == "unverified"
        assert d["columns_count"] is not None and d["columns_count"] >= 1
        assert d["schema_hash"]
    assert data["page"] == 1 and data["has_more"] is False

    # pagination: size=2 → first page has_more, second page the rest.
    p1 = client.get(f"{API}/connections/{cid}/file-datasets?page=1&size=2").json()["data"]
    assert len(p1["datasets"]) == 2 and p1["has_more"] is True
    p2 = client.get(f"{API}/connections/{cid}/file-datasets?page=2&size=2").json()["data"]
    assert len(p2["datasets"]) == 1 and p2["has_more"] is False

    # pattern filter.
    filtered = client.get(f"{API}/connections/{cid}/file-datasets?pattern=order").json()["data"]
    assert [d["table_name"] for d in filtered["datasets"]] == ["ORDERS"]


def test_verify_file_datasets_persists_status(client, tmp_path):
    lake = tmp_path / "lake"
    lake.mkdir()
    _write_csv(str(lake / "orders.csv"), ["ORDER_ID", "AMOUNT"], [[1, 10]])
    _write_csv(str(lake / "customers.csv"), ["CUSTOMER_ID"], [[1]])
    cid = _file_connection_scan(client, str(lake))

    # verify just ORDERS.
    vr = client.post(
        f"{API}/connections/{cid}/file-datasets:verify", json={"datasets": ["ORDERS"]}
    )
    assert vr.status_code == 200, vr.text
    vdata = vr.json()["data"]
    assert vdata["verified"][0]["verification_status"] == "verified"
    assert vdata["verified"][0]["schema_hash"]

    # re-list → ORDERS is now verified (persisted), CUSTOMERS still unverified.
    again = client.get(f"{API}/connections/{cid}/file-datasets").json()["data"]
    by = {d["table_name"]: d for d in again["datasets"]}
    assert by["ORDERS"]["verification_status"] == "verified"
    assert by["CUSTOMERS"]["verification_status"] == "unverified"

    # include filters honor the stored state.
    only_unverified = client.get(
        f"{API}/connections/{cid}/file-datasets?include_verified=false"
    ).json()["data"]
    assert {d["table_name"] for d in only_unverified["datasets"]} == {"CUSTOMERS"}


def test_file_datasets_rejects_non_file_connection(client):
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "inline", "name": "c", "auth_method": "none", "coordinates": {}},
    ).json()["data"]["id"]
    res = client.get(f"{API}/connections/{conn}/file-datasets")
    assert res.status_code >= 400
