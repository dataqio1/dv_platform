"""Recursive same-database hash compare — real SQLite (DVHASH UDF), no mocks.

Both tables live in ONE local connection, so the engine queries both in-database and pulls
only divergent leaf chunks. We assert: identical tables read ZERO rows (pure checksum), and
a table with a few differences matches the pure `compare_rows` bucketing exactly.
"""

from __future__ import annotations

from dvp.connectors.local_sql import LocalSqlConnector
from dvp.testing_engine.recursive_hash import compare_recursive_hash, same_server, supported
from dvp.testing_engine.rowcompare import CompareOptions, compare_rows

COLS = [
    {"name": "ID", "data_type": "NUMBER"},
    {"name": "AMOUNT", "data_type": "NUMBER"},
    {"name": "STATUS", "data_type": "VARCHAR"},
]


def _rows(n, mutate=False):
    rows = [{"ID": i, "AMOUNT": i * 10, "STATUS": "OPEN"} for i in range(1, n + 1)]
    if mutate:
        rows[4]["AMOUNT"] = 99999  # ID=5 value differs
        rows = [r for r in rows if r["ID"] != 7]  # ID=7 source-only (missing in target)
        rows.append({"ID": n + 1, "AMOUNT": 123, "STATUS": "NEW"})  # target-only
    return rows


def _conn(src_rows, dst_rows):
    return LocalSqlConnector(
        {
            "database": "DB",
            "schema": "PUBLIC",
            "tables": {
                "ORDERS_SRC": {"columns": COLS, "rows": src_rows},
                "ORDERS_DST": {"columns": COLS, "rows": dst_rows},
            },
        }
    )


class _CountingConn:
    """Wraps a connector, recording how many data rows the engine reads back."""

    def __init__(self, inner):
        self.inner = inner
        self.dialect = inner.dialect
        self.rows_read = 0

    def execute(self, sql):
        res = self.inner.execute(sql)
        # a "data pull" selects real columns (not a COUNT/SUM/MIN/MAX aggregate)
        if "COUNT(*)" not in sql and "GROUP BY" not in sql and "MIN(" not in sql:
            self.rows_read += len(res.rows)
        return res


def test_supported_and_same_server():
    c = _conn(_rows(3), _rows(3))
    opts = CompareOptions()
    assert supported(c, c, ["ID"], opts)
    assert same_server(c, c)  # one connector queries both tables
    assert not same_server(c, _conn(_rows(3), _rows(3)))  # distinct in-memory sqlite → not same


def test_identical_tables_read_zero_rows(client=None):
    base = _rows(200)
    inner = _conn(base, list(base))
    conn = _CountingConn(inner)
    outcome, notes = compare_recursive_hash(
        conn, conn, src_from='"ORDERS_SRC"', dst_from='"ORDERS_DST"',
        src_predicate=None, dst_predicate=None, key=["ID"], columns=["AMOUNT", "STATUS"],
        column_types={"AMOUNT": "NUMBER", "STATUS": "VARCHAR"},
    )
    assert outcome.summary.matched_rows == 200
    assert outcome.summary.mismatched_rows == 0
    assert conn.rows_read == 0  # identical → pure checksum, no data pulled
    assert any("no rows read" in n for n in notes)


def test_recursive_matches_rowcompare():
    src, dst = _rows(200), _rows(200, mutate=True)
    conn = _conn(src, dst)
    types = {"AMOUNT": "NUMBER", "STATUS": "VARCHAR"}
    outcome, notes = compare_recursive_hash(
        conn, conn, src_from='"ORDERS_SRC"', dst_from='"ORDERS_DST"',
        src_predicate=None, dst_predicate=None, key=["ID"], columns=["AMOUNT", "STATUS"],
        column_types=types, leaf_limit=10,  # force real recursion (many small leaves)
    )
    mem = compare_rows(src, dst, key=["ID"], columns=["AMOUNT", "STATUS"], column_types=types)
    for f in ("matched_rows", "mismatched_rows", "src_only_rows", "dst_only_rows", "ambiguous_rows"):
        assert getattr(outcome.summary, f) == getattr(mem.summary, f), f
    assert outcome.summary.per_column_mismatch == mem.summary.per_column_mismatch == {"AMOUNT": 1}
    assert any("divergent leaf" in n for n in notes)


def test_service_auto_selects_recursive_for_same_connection(client):
    API = "/api/v1"
    coords = {
        "database": "DB", "schema": "PUBLIC",
        "tables": {
            "ORDERS_SRC": {"columns": COLS, "rows": _rows(50)},
            "ORDERS_DST": {"columns": COLS, "rows": _rows(50, mutate=True)},
        },
    }
    cid = client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "wh", "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "data", "name": "same-db",
            "source": {"connection_id": cid, "table": "ORDERS_SRC"},
            "target": {"connection_id": cid, "table": "ORDERS_DST"},  # SAME connection
            "options": {"key": ["ID"]},
        },
    ).json()["data"]
    res = client.post(f"{API}/compares/{job['id']}:run").json()["data"]
    assert res["status"] == "succeeded"
    assert res["result"]["summary"]["engine"] == "recursive_hash"
    s = res["result"]["summary"]
    assert s["value_difference_rows"] == 1 and s["source_only_rows"] == 1 and s["dst_only_rows"] == 1


def test_fail_fast_surfaced_in_api_summary(client):
    """Fail-fast early stop must reach the results API: stopped_early + fail_fast flags, and a
    partial 'processed X of Y' where X (keys_total) < Y (src_row_count)."""
    API = "/api/v1"
    src = [{"ID": i, "AMOUNT": i, "STATUS": "OPEN"} for i in range(1, 501)]
    dst = [{"ID": i, "AMOUNT": i + 1000000, "STATUS": "OPEN"} for i in range(1, 501)]  # every row differs
    coords = {
        "database": "DB", "schema": "PUBLIC",
        "tables": {
            "ORDERS_SRC": {"columns": COLS, "rows": src},
            "ORDERS_DST": {"columns": COLS, "rows": dst},
        },
    }
    cid = client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "wh2", "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "data", "name": "failfast-api",
            "source": {"connection_id": cid, "table": "ORDERS_SRC"},
            "target": {"connection_id": cid, "table": "ORDERS_DST"},
            # small bad_record_limit => small leaves => fail-fast trips partway (as on a large table)
            "options": {"key": ["ID"], "fail_fast": True, "max_diff_rows": 50, "bad_record_limit": 20},
        },
    ).json()["data"]
    s = client.post(f"{API}/compares/{job['id']}:run").json()["data"]["result"]["summary"]
    assert s["stopped_early"] is True          # fail-fast banner condition in the results UI
    assert s["fail_fast"] is True
    assert s["keys_total"] < s["src_row_count"]  # processed X of Y, X < Y
    assert s["total_diff_rows"] >= 50


def test_recursive_reads_far_less_than_full_table():
    # 300 rows, a single differing row → the engine should pull only a small leaf, not 600 rows
    src, dst = _rows(300), _rows(300)
    dst[150]["AMOUNT"] = -1  # one diff deep in the table
    conn = _CountingConn(_conn(src, dst))
    outcome, _ = compare_recursive_hash(
        conn, conn, src_from='"ORDERS_SRC"', dst_from='"ORDERS_DST"',
        src_predicate=None, dst_predicate=None, key=["ID"], columns=["AMOUNT", "STATUS"],
        column_types={"AMOUNT": "NUMBER", "STATUS": "VARCHAR"}, leaf_limit=16,
    )
    assert outcome.summary.mismatched_rows == 1
    assert conn.rows_read < 120  # far fewer than the 600 rows a full pull would read


# ----------------------------- fail-fast / budget guards -----------------------------
def _diff_tables(n, offset=1000000):
    """n rows where EVERY row's AMOUNT differs between source and target (widespread diffs)."""
    src = [{"ID": i, "AMOUNT": i, "STATUS": "OPEN"} for i in range(1, n + 1)]
    dst = [{"ID": i, "AMOUNT": i + offset, "STATUS": "OPEN"} for i in range(1, n + 1)]
    return src, dst


def test_fail_fast_stops_on_widespread_differences():
    src, dst = _diff_tables(500)
    conn = _conn(src, dst)
    opts = CompareOptions(fail_fast=True, max_diff_rows=50, bad_record_limit=10)
    outcome, notes = compare_recursive_hash(
        conn, conn, src_from='"ORDERS_SRC"', dst_from='"ORDERS_DST"',
        src_predicate=None, dst_predicate=None, key=["ID"], columns=["AMOUNT", "STATUS"],
        column_types={"AMOUNT": "NUMBER", "STATUS": "VARCHAR"}, leaf_limit=20, options=opts,
    )
    assert any("stopped early" in n and "fail-fast" in n for n in notes)
    s = outcome.summary
    assert s.stopped_early is True           # structured flag the results UI reads
    diffs = s.mismatched_rows + s.src_only_rows + s.dst_only_rows + s.ambiguous_rows
    assert diffs >= 50                       # tripped once enough differences were confirmed
    assert s.matched_rows + diffs < 500      # did NOT examine the whole table
    assert s.keys_total < s.src_row_count    # "processed X of Y": X < Y when it bailed


def test_query_budget_aborts_and_is_inconclusive():
    src, dst = _diff_tables(500)
    conn = _conn(src, dst)
    opts = CompareOptions(max_queries=6)  # trips during descent, before leaves
    outcome, notes = compare_recursive_hash(
        conn, conn, src_from='"ORDERS_SRC"', dst_from='"ORDERS_DST"',
        src_predicate=None, dst_predicate=None, key=["ID"], columns=["AMOUNT"],
        column_types={"AMOUNT": "NUMBER"}, leaf_limit=10, options=opts,
    )
    assert any("stopped early" in n and "query budget" in n for n in notes)
    s = outcome.summary
    # bailed before confirming any difference → inconclusive, never a pass
    inconclusive = s.ambiguous_rows >= 1 or (s.mismatched_rows + s.src_only_rows + s.dst_only_rows) > 0
    assert inconclusive


def test_deadline_aborts():
    import time as _t
    src, dst = _diff_tables(300)
    conn = _conn(src, dst)
    opts = CompareOptions(deadline_epoch=_t.time() - 1)  # already past
    outcome, notes = compare_recursive_hash(
        conn, conn, src_from='"ORDERS_SRC"', dst_from='"ORDERS_DST"',
        src_predicate=None, dst_predicate=None, key=["ID"], columns=["AMOUNT"],
        column_types={"AMOUNT": "NUMBER"}, leaf_limit=10, options=opts,
    )
    assert any("time budget" in n for n in notes)


def test_no_guards_still_does_full_compare():
    """Without fail_fast/budget, a few differences are compared in full (regression guard)."""
    src, dst = _rows(200), _rows(200, mutate=True)
    conn = _conn(src, dst)
    types = {"AMOUNT": "NUMBER", "STATUS": "VARCHAR"}
    outcome, notes = compare_recursive_hash(
        conn, conn, src_from='"ORDERS_SRC"', dst_from='"ORDERS_DST"',
        src_predicate=None, dst_predicate=None, key=["ID"], columns=["AMOUNT", "STATUS"],
        column_types=types, leaf_limit=10,
    )
    assert not any("stopped early" in n for n in notes)
    assert outcome.summary.stopped_early is False  # full compare → not an early stop
    assert outcome.summary.per_column_mismatch == {"AMOUNT": 1}


def test_cancel_propagates_as_cancelled():
    """A user cancel (should_cancel→True) raises CompareCancelled out of the engine (so the
    runner can mark the job 'cancelled'), not a swallowed stopped-early result."""
    import pytest

    from dvp.testing_engine.cancel import CompareCancelled

    src, dst = _diff_tables(500)
    conn = _conn(src, dst)
    state = {"n": 0}

    def should_cancel():
        state["n"] += 1
        return state["n"] > 3  # cancel after a few queries

    opts = CompareOptions(should_cancel=should_cancel)
    with pytest.raises(CompareCancelled):
        compare_recursive_hash(
            conn, conn, src_from='"ORDERS_SRC"', dst_from='"ORDERS_DST"',
            src_predicate=None, dst_predicate=None, key=["ID"], columns=["AMOUNT"],
            column_types={"AMOUNT": "NUMBER"}, leaf_limit=10, options=opts,
        )


def test_recursive_hash_catches_large_numeric_difference():
    """The pushdown checksum must NOT collapse two distinct 18-digit NUMBERs to the same hash (a
    float round-trip did) — the differing row must be caught, not pruned as matched."""
    cols = [{"name": "ID", "data_type": "NUMBER"}, {"name": "AMT", "data_type": "NUMBER"}]
    src = [{"ID": i, "AMT": 100000000000000000 + i} for i in range(1, 51)]
    dst = [dict(r) for r in src]
    dst[25]["AMT"] += 1  # ID=26: AMT differs only in the last digit of an 18-digit value
    conn = LocalSqlConnector({
        "database": "DB", "schema": "PUBLIC",
        "tables": {"ORDERS_SRC": {"columns": cols, "rows": src},
                   "ORDERS_DST": {"columns": cols, "rows": dst}},
    })
    outcome, _ = compare_recursive_hash(
        conn, conn, src_from='"ORDERS_SRC"', dst_from='"ORDERS_DST"',
        src_predicate=None, dst_predicate=None, key=["ID"], columns=["AMT"],
        column_types={"AMT": "NUMBER"}, leaf_limit=10,
    )
    assert outcome.summary.mismatched_rows == 1
    assert outcome.summary.per_column_mismatch == {"AMT": 1}
