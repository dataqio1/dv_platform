"""FileConnector: read CSV/JSON/NDJSON (+Parquet if pyarrow) from local paths; compare a
file against a table. S3 uses the same code path (lazy boto3); not exercised here."""

from __future__ import annotations

import json

import pytest

from dvp.connectors.base import TableRef
from dvp.connectors.file_source import FileConnector, _infer_type
from dvp.connectors.registry import build_connector

API = "/api/v1"


def _tref(name="ORDERS"):
    return TableRef("LAKE", "RAW", name)


def _write(p, text):
    p.write_text(text)
    return str(p)


def test_registry_builds_file_connector(tmp_path):
    uri = _write(tmp_path / "t.csv", "ID\n1\n")
    assert isinstance(
        build_connector("file", {"uri": uri, "table": "T"}, "none", None), FileConnector
    )


def test_infer_type():
    assert _infer_type([1, 2, 3]) == "NUMBER"
    assert _infer_type([1.5, 2.0]) == "NUMBER"
    assert _infer_type(["2026-09-18T10:45:00", "2026-09-19T00:00:00"]) == "TIMESTAMP"
    assert _infer_type(["2026-09-18", "2026-09-19"]) == "DATE"
    assert _infer_type(["a@x.com", "b@x.com"]) == "VARCHAR"


def test_csv_file(tmp_path):
    uri = _write(
        tmp_path / "orders.csv", "ORDER_ID,AMOUNT,STATUS\n1,10,OPEN\n2,-5,CLOSED\n3,20,OPEN\n"
    )
    c = FileConnector({"uri": uri})  # table name inferred from filename → ORDERS
    assert c.list_tables() == ["ORDERS"]
    schema = {col.name: col.data_type for col in c.get_schema(_tref())}
    assert schema["ORDER_ID"] == "NUMBER" and schema["STATUS"] == "VARCHAR"
    assert c.count(_tref()) == 3
    # real SQL over the file (numeric coercion makes this work)
    assert c.execute('SELECT COUNT(*) FROM "ORDERS" WHERE "AMOUNT" < 0').rows[0][0] == 1


def test_ndjson_and_json(tmp_path):
    nd = _write(tmp_path / "c.ndjson", '{"ID":1,"NAME":"a"}\n{"ID":2,"NAME":"b"}\n')
    c = FileConnector({"uri": nd, "table": "CUST"})
    assert c.count(TableRef("", "", "CUST")) == 2

    js = _write(tmp_path / "d.json", json.dumps([{"ID": 1}, {"ID": 2}, {"ID": 3}]))
    c2 = FileConnector({"uri": js, "table": "D"})
    assert c2.count(TableRef("", "", "D")) == 3


def test_parquet_if_available(tmp_path):
    pq = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pqw

    tbl = pq.table({"ID": [1, 2], "AMOUNT": [10.0, 20.0]})
    path = str(tmp_path / "o.parquet")
    pqw.write_table(tbl, path)
    c = FileConnector({"uri": path, "table": "O"})
    assert c.count(TableRef("", "", "O")) == 2
    assert c.get_schema(TableRef("", "", "O"))[0].name == "ID"


def test_compare_file_vs_table(client, tmp_path):
    # a CSV file on disk vs a local SQL "table" — one value differs, one dst-only
    csv_uri = _write(
        tmp_path / "orders.csv",
        "ORDER_ID,AMOUNT,STATUS\n1,10,OPEN\n2,20,OPEN\n3,30,CLOSED\n",
    )
    file_conn = client.post(
        f"{API}/connections",
        json={
            "source_type": "file",
            "name": "orders-file",
            "auth_method": "none",
            "coordinates": {"uri": csv_uri, "table": "ORDERS"},
        },
    ).json()["data"]
    tbl_conn = client.post(
        f"{API}/connections",
        json={
            "source_type": "local",
            "name": "orders-db",
            "auth_method": "none",
            "coordinates": {
                "database": "DB",
                "schema": "PUBLIC",
                "tables": {
                    "ORDERS": {
                        "columns": [
                            {"name": "ORDER_ID", "data_type": "NUMBER"},
                            {"name": "AMOUNT", "data_type": "NUMBER"},
                            {"name": "STATUS", "data_type": "VARCHAR"},
                        ],
                        "rows": [
                            {"ORDER_ID": 1, "AMOUNT": 10, "STATUS": "OPEN"},
                            {"ORDER_ID": 2, "AMOUNT": 25, "STATUS": "OPEN"},  # AMOUNT differs
                            {"ORDER_ID": 4, "AMOUNT": 40, "STATUS": "NEW"},
                        ],
                    }
                },
            },
        },
    ).json()["data"]

    job = client.post(
        f"{API}/compares",
        json={
            "kind": "data",
            "name": "file vs table",
            "source": {"connection_id": file_conn["id"], "table": "ORDERS"},
            "target": {"connection_id": tbl_conn["id"], "table": "ORDERS"},
        },
    ).json()["data"]
    res = client.post(f"{API}/compares/{job['id']}:run").json()["data"]
    s = res["result"]["summary"]
    assert res["result"]["detected_key"] == ["ORDER_ID"]
    assert s["matched_rows"] == 1  # ORDER_ID 1
    assert s["value_difference_rows"] == 1  # ORDER_ID 2 (AMOUNT 20 vs 25)
    assert s["source_only_rows"] == 1  # ORDER_ID 3 (file only)
    assert s["target_only_rows"] == 1  # ORDER_ID 4 (table only)


# ---- source-independence: all four capabilities on a FILE source (no mocks) ----
def _file_conn(client, tmp_path, name, body):
    uri = _write(tmp_path / f"{name}.csv", body)
    return client.post(
        f"{API}/connections",
        json={
            "source_type": "file",
            "name": name,
            "auth_method": "none",
            "coordinates": {"uri": uri, "table": "ORDERS"},
        },
    ).json()["data"]["id"]


def test_count_and_schema_compare_on_file(client, tmp_path):
    body = "ORDER_ID,AMOUNT\n1,10\n2,20\n3,30\n"
    a = _file_conn(client, tmp_path, "a", body)
    b = _file_conn(client, tmp_path, "b", body)
    for kind in ("count", "schema"):
        job = client.post(
            f"{API}/compares",
            json={
                "kind": kind,
                "source": {"connection_id": a, "table": "ORDERS"},
                "target": {"connection_id": b, "table": "ORDERS"},
            },
        ).json()["data"]
        res = client.post(f"{API}/compares/{job['id']}:run").json()["data"]
        assert res["result"]["summary"]["match"] is True, kind


def test_data_quality_on_file_source(client, tmp_path):
    cid = _file_conn(
        client, tmp_path, "dq", "ORDER_ID,AMOUNT,EMAIL\n1,10,a@x.com\n2,-5,\n3,20,c@x.com\n"
    )
    mid = client.post(
        f"{API}/monitors",
        json={"connection_id": cid, "name": "file-dq", "scope": {"tables": ["ORDERS"]}},
    ).json()["data"]["id"]
    client.post(
        f"{API}/monitors/{mid}/rules",
        json={
            "name": "no_negative",
            "type": "custom_sql",
            "table_ref": "ORDERS",
            "severity": "critical",
            "config": {
                "expression": 'SELECT COUNT(*) AS v FROM {{table}} WHERE "AMOUNT" < 0',
                "threshold": 0,
            },
        },
    )
    summary = client.post(f"{API}/monitors/{mid}:run").json()["data"]
    assert summary["rules_evaluated"] == 1

    cards = client.get(f"{API}/monitors/{mid}/tables/ORDERS/pillars").json()["data"]
    by = {c["pillar"]: c for c in cards["pillars"]}
    assert by["volume"]["headline"] == "3 rows"  # data quality profiling on a FILE
    assert by["schema"]["headline"] == "3 columns"
    assert by["quality"]["configured"] is True

    rules = client.get(f"{API}/monitors/{mid}/tables/ORDERS/rules").json()["data"]
    assert (
        rules[0]["latest"]["passed"] is False
    )  # one negative amount, caught via pushdown SQL on the file


def test_scan_prefix_folders_and_files(tmp_path):
    """scan=true: each file or sub-folder one level below becomes a table; a sub-folder
    of part-files loads as a single unioned table; Spark markers (_SUCCESS) are skipped."""
    (tmp_path / "orders").mkdir()
    _write(tmp_path / "orders" / "part-0000.csv", "ORDER_ID,AMOUNT\n1,10\n2,20\n")
    _write(tmp_path / "orders" / "part-0001.csv", "ORDER_ID,AMOUNT\n3,30\n")
    _write(tmp_path / "orders" / "_SUCCESS", "")  # marker must be ignored
    (tmp_path / "customers").mkdir()
    _write(tmp_path / "customers" / "c.ndjson", '{"CUST_ID":1}\n{"CUST_ID":2}\n')
    _write(tmp_path / "regions.csv", "REGION,CODE\nUS,1\nEU,2\n")  # loose file → table

    c = FileConnector({"uri": str(tmp_path), "scan": True})
    assert c.list_tables() == ["CUSTOMERS", "ORDERS", "REGIONS"]
    # folder of part-files unions to one table
    assert c.count(_tref("ORDERS")) == 3
    assert {r[0] for r in c.execute('SELECT "ORDER_ID" FROM "ORDERS"').rows} == {1, 2, 3}
    assert c.count(_tref("CUSTOMERS")) == 2
    assert c.count(_tref("REGIONS")) == 2
