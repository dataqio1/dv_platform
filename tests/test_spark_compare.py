"""Spark data-compare engine: parity with the pure engine + a service-level Spark run.

Skipped unless pyspark is installed (Java required). One shared SparkSession."""

from __future__ import annotations

import pytest

pytest.importorskip("pyspark")

from dvp.testing_engine.rowcompare import CompareOptions, compare_rows  # noqa: E402
from dvp.testing_engine.service import _rows_to_df  # noqa: E402
from dvp.testing_engine.spark_compare import compare_spark  # noqa: E402

API = "/api/v1"

# ID1 match · ID2 A differs · ID3 source-only · ID4 target-only
SRC = [
    {"ID": 1, "A": 10, "B": "x"},
    {"ID": 2, "A": 20, "B": "y"},
    {"ID": 3, "A": 30, "B": "z"},
]
DST = [
    {"ID": 1, "A": 10, "B": "x"},
    {"ID": 2, "A": 25, "B": "y"},
    {"ID": 4, "A": 40, "B": "w"},
]
TYPES = {"A": "NUMBER", "B": "VARCHAR"}


@pytest.fixture(scope="session")
def spark():
    from dvp.abstractions.spark import SparkSessionProvider

    s = SparkSessionProvider(master="local[1]", app_name="dvp-test").get()
    yield s
    s.stop()


def test_spark_matches_inmemory(spark):
    cols, key, cmp = ["ID", "A", "B"], ["ID"], ["A", "B"]
    sdf = _rows_to_df(spark, SRC, cols)
    ddf = _rows_to_df(spark, DST, cols)
    sp = compare_spark(spark, sdf, ddf, key=key, columns=cmp, column_types=TYPES)
    mem = compare_rows(SRC, DST, key=key, columns=cmp, column_types=TYPES)

    for f in (
        "matched_rows",
        "mismatched_rows",
        "src_only_rows",
        "dst_only_rows",
        "ambiguous_rows",
        "keys_total",
    ):
        assert getattr(sp.summary, f) == getattr(mem.summary, f), f
    assert sp.summary.per_column_mismatch == mem.summary.per_column_mismatch == {"A": 1}

    # value_difference sample carries ALL columns with a differs flag
    vd = next(d for d in sp.differences if d.bucket == "value_difference")
    assert vd.key == {"ID": "2"}
    by = {c.column: c for c in vd.cells}
    assert by["A"].differs is True and by["B"].differs is False


def test_spark_ambiguous_bucket(spark):
    src = [{"ID": 1, "A": 10}, {"ID": 1, "A": 11}]  # two rows share key 1
    dst = [{"ID": 1, "A": 10}]
    sp = compare_spark(
        spark,
        _rows_to_df(spark, src, ["ID", "A"]),
        _rows_to_df(spark, dst, ["ID", "A"]),
        key=["ID"],
        columns=["A"],
        column_types={"A": "NUMBER"},
    )
    assert sp.summary.ambiguous_rows == 1
    amb = next(d for d in sp.differences if d.bucket == "ambiguous")
    assert amb.src_count == 2 and amb.dst_count == 1


def test_tolerance_on_spark(spark):
    src = [{"ID": 1, "A": 100}]
    dst = [{"ID": 1, "A": 101}]
    opts = CompareOptions(tolerance=2.0)
    sp = compare_spark(
        spark,
        _rows_to_df(spark, src, ["ID", "A"]),
        _rows_to_df(spark, dst, ["ID", "A"]),
        key=["ID"],
        columns=["A"],
        column_types={"A": "NUMBER"},
        options=opts,
    )
    assert sp.summary.matched_rows == 1  # within tolerance → match


def test_service_runs_on_spark(client):
    coords = {
        "database": "DB",
        "schema": "PUBLIC",
        "tables": {
            "ORDERS": {
                "columns": [
                    {"name": "ORDER_ID", "data_type": "NUMBER"},
                    {"name": "AMOUNT", "data_type": "NUMBER"},
                ],
                "rows": [{"ORDER_ID": 1, "AMOUNT": 10}, {"ORDER_ID": 2, "AMOUNT": 20}],
            }
        },
    }
    coords2 = {
        **coords,
        "tables": {
            "ORDERS": {
                **coords["tables"]["ORDERS"],
                "rows": [{"ORDER_ID": 1, "AMOUNT": 10}, {"ORDER_ID": 2, "AMOUNT": 99}],
            }
        },
    }
    s = client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "s", "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]
    t = client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "t", "auth_method": "none", "coordinates": coords2},
    ).json()["data"]["id"]
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "data",
            "name": "spark run",
            "source": {"connection_id": s, "table": "ORDERS"},
            "target": {"connection_id": t, "table": "ORDERS"},
            "options": {"key": ["ORDER_ID"], "engine": "spark"},
        },
    ).json()["data"]
    res = client.post(f"{API}/compares/{job['id']}:run").json()["data"]
    assert res["status"] == "succeeded"
    assert res["result"]["summary"]["engine"] == "spark"
    assert res["result"]["summary"]["matched_rows"] == 1
    assert res["result"]["summary"]["value_difference_rows"] == 1
