"""Compare exception rows stay in the customer's warehouse, not on this server."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dvp.abstractions.result_store import WarehouseResultStore
from dvp.connectors.base import QueryResult
from dvp.core.errors import PermissionDeniedError

API = "/api/v1"


def _cols():
    return [
        {"name": "ID", "data_type": "NUMBER", "nullable": False},
        {"name": "NOTE", "data_type": "STRING", "nullable": True},
    ]


def _conn(client, name, rows, *, path=None):
    coordinates = {"database": "D", "schema": "S", "tables": {}}
    if path is None:
        coordinates["tables"] = {"ORDERS": {"columns": _cols(), "rows": rows}}
    else:
        coordinates["sqlite_path"] = str(path)
    return client.post(
        f"{API}/connections",
        json={
            "source_type": "local",
            "name": name,
            "auth_method": "none",
            "coordinates": coordinates,
        },
    ).json()["data"]["id"]


def test_snowflake_sql_names_the_customer_table_and_rejects_another_group():
    class Conn:
        dialect = "snowflake"

        def __init__(self):
            self.sql: list[str] = []

        def execute(self, sql: str) -> QueryResult:
            self.sql.append(sql)
            if sql.lstrip().upper().startswith("SELECT"):
                return QueryResult(columns=["PAYLOAD"], rows=[('{"key": {"ID": 1}}',)])
            return QueryResult(columns=[], rows=[])

    ctx = type("C", (), {"group_id": "grp-1"})()
    connector = Conn()
    store = WarehouseResultStore(
        connector,
        connection_id="conn-1",
        database="ANALYTICS",
        schema="DVP",
        table="DIFF_SAMPLES",
    )
    ref = store.write_detail(ctx, "compare_job", [{"key": {"ID": 1}, "bucket": "value_difference"}])
    assert ref == "warehouse://conn-1/ANALYTICS.DVP.DIFF_SAMPLES/grp-1/compare_job"
    sql = "\n".join(connector.sql)
    assert '"ANALYTICS"."DVP"."DIFF_SAMPLES"' in sql
    assert "grp-1/compare_job" in sql
    assert store.read_detail(ctx, ref) == [{"key": {"ID": 1}}]
    other = type("C", (), {"group_id": "grp-2"})()
    with pytest.raises(PermissionDeniedError):
        store.read_detail(other, ref)


def test_exception_rows_are_written_to_the_customer_table(client, settings, tmp_path):
    warehouse = tmp_path / "customer.db"
    src = _conn(client, "src", [{"ID": 1, "NOTE": "ok"}])
    dst = _conn(client, "dst", [{"ID": 1, "NOTE": "LEAK-ME"}])
    hold = _conn(client, "hold", [], path=warehouse)

    current = client.get(f"{API}/settings/sample-store").json()["data"]
    assert current["backend"] == "local"
    assert current["location"] is None

    saved = client.put(
        f"{API}/settings/sample-store",
        json={
            "connection_id": hold,
            "database": "analytics",
            "schema": "dvp",
            "table": "diff_samples",
        },
    ).json()["data"]
    assert saved["location"] == "ANALYTICS.DVP.DIFF_SAMPLES"
    assert saved["connection_id"] == hold

    audit = client.get(f"{API}/audit?entity_type=group").json()["data"]
    assert any(
        row["action"] == "sample_store.update"
        and row["detail"]["after"]["location"] == "ANALYTICS.DVP.DIFF_SAMPLES"
        for row in audit
    )

    bad = client.put(
        f"{API}/settings/sample-store",
        json={"connection_id": hold, "database": "BAD NAME", "schema": "DVP", "table": "T"},
    )
    assert bad.status_code == 422

    batch = client.post(
        f"{API}/compares/batch",
        json={
            "kind": "data",
            "name": "cutover",
            "source": {"connection_id": src},
            "target": {"connection_id": dst},
            "tables": ["ORDERS"],
            "options": {"key": ["ID"]},
        },
    ).json()["data"]
    assert batch["matched"] == 0
    jobs = client.get(f"{API}/compares/batches/{batch['batch_id']}").json()["data"]["jobs"]
    job = client.get(f"{API}/compares/{jobs[0]['id']}").json()["data"]
    assert job["result"]["sample_location"] == "ANALYTICS.DVP.DIFF_SAMPLES"
    assert job["result"]["detail_ref"].startswith("warehouse://")
    assert "LEAK-ME" not in str(job)

    rows = client.get(f"{API}/compares/{jobs[0]['id']}/differences").json()["data"]
    assert any("LEAK-ME" in str(row) for row in rows)

    with sqlite3.connect(warehouse) as db:
        stored = db.execute("SELECT PAYLOAD FROM DIFF_SAMPLES").fetchall()
    assert stored and "LEAK-ME" in stored[0][0]

    result_dir = Path(settings.result_local_dir)
    leaked = [
        p
        for p in result_dir.rglob("*")
        if p.is_file() and "LEAK-ME" in p.read_text(encoding="utf-8")
    ]
    assert leaked == []

    signed = client.post(f"{API}/compares/batches/{batch['batch_id']}:sign-off").json()["data"]
    report = client.get(f"{API}/reports/{signed['id']}").json()["data"]
    assert "ID=1" in str(report["document"])
    assert "LEAK-ME" not in str(report["document"])

    client.delete(f"{API}/compares/batches/{batch['batch_id']}")
    with sqlite3.connect(warehouse) as db:
        left = db.execute("SELECT COUNT(*) FROM DIFF_SAMPLES").fetchone()[0]
    assert left == 0
