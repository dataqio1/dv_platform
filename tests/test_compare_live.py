"""Live count/schema/data compare over real local SQLite connectors (no mocks).

Covers V1 (count + schema) and V3-local (cell-level data compare via row-pull +
compare_rows) and key auto-detection. Scenarios V-S1/2/4/5/7/8."""

from __future__ import annotations

API = "/api/v1"


def _cols(*specs):
    return [{"name": n, "data_type": t, "nullable": nul} for n, t, nul in specs]


SRC_TABLE = {
    "columns": _cols(
        ("ORDER_ID", "NUMBER", False),
        ("AMOUNT", "NUMBER", False),
        ("STATUS", "VARCHAR", True),
    ),
    "rows": [
        {"ORDER_ID": 1, "AMOUNT": 10, "STATUS": "OPEN"},
        {"ORDER_ID": 2, "AMOUNT": 20, "STATUS": "OPEN"},
        {"ORDER_ID": 3, "AMOUNT": 30, "STATUS": "CLOSED"},
    ],
}
# target: row 2 AMOUNT differs, row 3 missing, row 4 extra, STATUS retyped + new col
DST_TABLE = {
    "columns": _cols(
        ("ORDER_ID", "NUMBER", False),
        ("AMOUNT", "DECIMAL", False),  # NUMBER↔DECIMAL → canonical match
        ("STATUS", "VARCHAR", True),
        ("REGION", "VARCHAR", True),  # target-only column
    ),
    "rows": [
        {"ORDER_ID": 1, "AMOUNT": 10, "STATUS": "OPEN", "REGION": "EU"},
        {"ORDER_ID": 2, "AMOUNT": 25, "STATUS": "OPEN", "REGION": "EU"},  # AMOUNT differs
        {"ORDER_ID": 4, "AMOUNT": 40, "STATUS": "NEW", "REGION": "US"},  # dst-only
    ],
}


def _conn(client, name, table_name, spec):
    coords = {"database": "DB", "schema": "PUBLIC", "tables": {table_name: spec}}
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": name, "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]


def _run(client, kind, scid, dcid, table="ORDERS", options=None):
    job = client.post(
        f"{API}/compares",
        json={
            "kind": kind,
            "name": f"{kind} test",
            "source": {"connection_id": scid, "table": table},
            "target": {"connection_id": dcid, "table": table},
            "options": options or {},
        },
    ).json()["data"]
    assert job["status"] == "queued"
    return client.post(f"{API}/compares/{job['id']}:run").json()["data"], job["id"]


def test_count_compare(client):
    scid = _conn(client, "src", "ORDERS", SRC_TABLE)
    dcid = _conn(client, "dst", "ORDERS", DST_TABLE)
    res, _ = _run(client, "count", scid, dcid)
    summary = res["result"]["summary"]
    assert summary["src_count"] == 3
    assert summary["dst_count"] == 3
    assert summary["match"] is True  # equal counts


def test_count_compare_tolerance(client):
    scid = _conn(client, "src", "ORDERS", SRC_TABLE)
    # dst has one fewer row
    dst = {"columns": SRC_TABLE["columns"], "rows": SRC_TABLE["rows"][:2]}
    dcid = _conn(client, "dst", "ORDERS", dst)
    exact, _ = _run(client, "count", scid, dcid, options={"tolerance_pct": 0})
    assert exact["result"]["summary"]["match"] is False  # 3 vs 2
    tol, _ = _run(client, "count", scid, dcid, options={"tolerance_pct": 50})
    assert tol["result"]["summary"]["match"] is True  # 33% diff within 50%


def test_schema_compare_detects_drift(client):
    scid = _conn(client, "src", "ORDERS", SRC_TABLE)
    dcid = _conn(client, "dst", "ORDERS", DST_TABLE)
    res, jid = _run(client, "schema", scid, dcid)
    summary = res["result"]["summary"]
    assert summary["target_only"] == 1  # REGION
    assert summary["match"] is False
    # AMOUNT NUMBER↔DECIMAL is a canonical match (not a type mismatch)
    recs = client.get(f"{API}/compares/{jid}/differences").json()["data"]
    amount = next(r for r in recs if r["column"] == "AMOUNT")
    assert amount["canonical_match"] is True
    region = next(r for r in recs if r["column"] == "REGION")
    assert region["status"] == "target_only"


def test_data_compare_cell_level_with_autodetect_key(client):
    scid = _conn(client, "src", "ORDERS", SRC_TABLE)
    dcid = _conn(client, "dst", "ORDERS", DST_TABLE)
    res, jid = _run(client, "data", scid, dcid)  # no key → auto-detect ORDER_ID
    summary = res["result"]["summary"]
    assert res["result"]["detected_key"] == ["ORDER_ID"]
    assert summary["matched_rows"] == 1  # ORDER_ID 1
    assert summary["mismatched_rows"] == 1  # ORDER_ID 2 (AMOUNT 20 vs 25)
    assert summary["src_only_rows"] == 1  # ORDER_ID 3
    assert summary["dst_only_rows"] == 1  # ORDER_ID 4
    assert summary["per_column_mismatch"].get("AMOUNT") == 1

    # bucketed drill-down: value_difference (bucket 1)
    diffs = client.get(f"{API}/compares/{jid}/differences?bucket=value_difference").json()["data"]
    assert diffs[0]["key"] == {"ORDER_ID": 2}
    assert diffs[0]["cells"][0]["column"] == "AMOUNT"
    # source_only (bucket 2) carries the record
    so = client.get(f"{API}/compares/{jid}/differences?bucket=source_only").json()["data"]
    assert so[0]["key"] == {"ORDER_ID": 3}
    # pass records sampled too
    good = client.get(f"{API}/compares/{jid}/differences?bucket=match").json()["data"]
    assert good[0]["record"]["ORDER_ID"] == 1


# ----------------------------- table/view/SQL on either side (any combination) -----------------------------
def test_data_compare_sql_source_vs_table_target(client):
    scid = _conn(client, "src", "ORDERS", SRC_TABLE)
    dcid = _conn(client, "dst", "ORDERS", DST_TABLE)
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "data",
            "name": "sql vs table",
            # source is an arbitrary SQL query; target is a table
            "source": {
                "connection_id": scid,
                "sql": 'SELECT "ORDER_ID","AMOUNT","STATUS" FROM "ORDERS"',
            },
            "target": {"connection_id": dcid, "table": "ORDERS"},
            "options": {"key": ["ORDER_ID"], "columns": ["AMOUNT", "STATUS"]},
        },
    ).json()["data"]
    res = client.post(f"{API}/compares/{job['id']}:run").json()["data"]
    assert res["status"] == "succeeded", res.get("error")
    s = res["result"]["summary"]
    assert s["matched_rows"] == 1  # ORDER_ID 1
    assert s["value_difference_rows"] == 1  # ORDER_ID 2 (AMOUNT 20 vs 25)
    assert s["source_only_rows"] == 1  # ORDER_ID 3
    assert s["target_only_rows"] == 1  # ORDER_ID 4


def test_count_compare_sql_both_sides(client):
    cid = _conn(client, "c", "ORDERS", SRC_TABLE)  # 3 rows
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "count",
            "name": "sql vs sql",
            "source": {"connection_id": cid, "sql": 'SELECT * FROM "ORDERS" WHERE "ORDER_ID" <= 2'},
            "target": {"connection_id": cid, "sql": 'SELECT * FROM "ORDERS"'},
        },
    ).json()["data"]
    res = client.post(f"{API}/compares/{job['id']}:run").json()["data"]
    s = res["result"]["summary"]
    assert s["src_count"] == 2 and s["dst_count"] == 3
    assert s["match"] is False


def test_schema_compare_sql_side_infers_columns(client):
    cid = _conn(client, "c", "ORDERS", SRC_TABLE)
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "schema",
            "name": "sql schema",
            "source": {"connection_id": cid, "sql": 'SELECT "ORDER_ID","AMOUNT" FROM "ORDERS"'},
            "target": {"connection_id": cid, "table": "ORDERS"},
        },
    ).json()["data"]
    res = client.post(f"{API}/compares/{job['id']}:run").json()["data"]
    s = res["result"]["summary"]
    # the SQL projects 2 of 3 columns → STATUS is target_only
    assert s["target_only"] == 1


# ----------------------------- batch compare (up to 100 table pairs) -----------------------------
def test_batch_compare_partial(client):
    cols = _cols(("ID", "NUMBER", False), ("V", "NUMBER", False))

    def multi_conn(name, orders, custs):
        coords = {
            "database": "D",
            "schema": "S",
            "tables": {
                "ORDERS": {"columns": cols, "rows": orders},
                "CUSTOMERS": {"columns": cols, "rows": custs},
            },
        }
        return client.post(
            f"{API}/connections",
            json={
                "source_type": "local",
                "name": name,
                "auth_method": "none",
                "coordinates": coords,
            },
        ).json()["data"]["id"]

    src = multi_conn("bsrc", [{"ID": 1, "V": 10}], [{"ID": 1, "V": 5}])
    dst = multi_conn(
        "bdst", [{"ID": 1, "V": 10}], [{"ID": 1, "V": 9}]
    )  # ORDERS match, CUSTOMERS differ

    r = client.post(
        f"{API}/compares/batch",
        json={
            "kind": "data",
            "name": "nightly",
            "source": {"connection_id": src},
            "target": {"connection_id": dst},
            "tables": ["ORDERS", "CUSTOMERS", "MISSING"],  # MISSING table → fails, batch continues
            "options": {"key": ["ID"]},
        },
    ).json()["data"]
    assert r["total"] == 3
    assert r["succeeded"] == 2
    assert r["failed"] == 1  # MISSING table
    assert r["status"] == "partial"  # one bad table doesn't sink the batch
    assert r["matched"] == 1  # ORDERS matches; CUSTOMERS differs

    batches = client.get(f"{API}/compares/batches").json()["data"]
    assert any(b["batch_id"] == r["batch_id"] and b["total"] == 3 for b in batches)
    detail = client.get(f"{API}/compares/batches/{r['batch_id']}").json()["data"]
    assert len(detail["jobs"]) == 3


def test_batch_limit_100(client):
    cid = _conn(client, "c", "ORDERS", SRC_TABLE)
    r = client.post(
        f"{API}/compares/batch",
        json={
            "kind": "count",
            "name": "big",
            "source": {"connection_id": cid},
            "target": {"connection_id": cid},
            "tables": [f"T{i}" for i in range(101)],
        },
    )
    assert r.status_code == 422  # over the 100-table limit
