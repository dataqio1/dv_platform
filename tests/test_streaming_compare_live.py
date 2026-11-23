"""Streaming key-range compare (the cross-database floor): bounded windows over the key space,
exact results identical to the in-memory engine, plus live progress. Real API + real SQLite-backed
connectors, no mocks. A tiny `stream_window` forces many windows so boundary + tail logic is exercised."""

from __future__ import annotations

from dvp.db.models import CompareJob
from dvp.testing_engine.service import CompareService, _stream_window_rows

API = "/api/v1"

COLS = [{"name": "ORDER_ID", "data_type": "NUMBER"}, {"name": "AMOUNT", "data_type": "NUMBER"}]


def _conn(client, name, rows):
    coords = {"database": "DB", "schema": "PUBLIC", "tables": {"ORDERS": {"columns": COLS, "rows": rows}}}
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": name, "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]


def _run(client, src, dst, options):
    body = {
        "kind": "data", "name": "stream",
        "source": {"connection_id": src, "table": "ORDERS"},
        "target": {"connection_id": dst, "table": "ORDERS"},
        "options": {"key": ["ORDER_ID"], **options},
    }
    jid = client.post(f"{API}/compares", json=body).json()["data"]["id"]
    client.post(f"{API}/compares/{jid}:run")
    return client.get(f"{API}/compares/{jid}").json()["data"]


# Source keys 1..5; target: 2 differs, 4 missing (source_only), 6 & 7 are extra AND beyond the last
# source key (exercises the target-only TAIL sweep). Everything else matches.
SRC = [{"ORDER_ID": i, "AMOUNT": i * 10} for i in (1, 2, 3, 4, 5)]
DST = [
    {"ORDER_ID": 1, "AMOUNT": 10}, {"ORDER_ID": 2, "AMOUNT": 999},  # 2 differs
    {"ORDER_ID": 3, "AMOUNT": 30},                                   # 4 missing
    {"ORDER_ID": 5, "AMOUNT": 50},
    {"ORDER_ID": 6, "AMOUNT": 60}, {"ORDER_ID": 7, "AMOUNT": 70},    # target-only tail
]


def test_streaming_matches_in_memory_results(client):
    src, dst = _conn(client, "st-src", SRC), _conn(client, "st-dst", DST)

    # Forced streaming with a tiny window (2 rows) → several windows + a tail sweep.
    streamed = _run(client, src, dst, {"engine": "streaming", "stream_window": 2})
    s = streamed["result"]["summary"]
    assert s["engine"] == "streaming"
    assert s["matched_rows"] == 3           # ids 1, 3, 5
    assert s["value_difference_rows"] == 1  # id 2
    assert s["source_only_rows"] == 1       # id 4
    assert s["target_only_rows"] == 2       # ids 6, 7 (found by the tail sweep)
    assert s["keys_total"] == 7
    assert s["match"] is False

    # Parity: the in-memory engine on the same data must agree bucket-for-bucket.
    src2, dst2 = _conn(client, "im-src", SRC), _conn(client, "im-dst", DST)
    mem = _run(client, src2, dst2, {"engine": "inmemory"})["result"]["summary"]
    for k in ("matched_rows", "value_difference_rows", "source_only_rows", "target_only_rows", "keys_total"):
        assert streamed["result"]["summary"][k] == mem[k], k


def test_streaming_identical_tables_all_match(client):
    rows = [{"ORDER_ID": i, "AMOUNT": i} for i in range(1, 51)]
    src, dst = _conn(client, "eq-src", rows), _conn(client, "eq-dst", list(rows))
    out = _run(client, src, dst, {"engine": "streaming", "stream_window": 7})["result"]["summary"]
    assert out["match"] is True
    assert out["matched_rows"] == 50 and out["keys_total"] == 50
    assert out["source_only_rows"] == 0 and out["target_only_rows"] == 0


def test_streaming_fail_fast_stops_early(client):
    # Every row differs; fail-fast should stop before scanning all of them.
    src_rows = [{"ORDER_ID": i, "AMOUNT": 1} for i in range(1, 101)]
    dst_rows = [{"ORDER_ID": i, "AMOUNT": 2} for i in range(1, 101)]
    src, dst = _conn(client, "ff-src", src_rows), _conn(client, "ff-dst", dst_rows)
    out = _run(client, src, dst, {
        "engine": "streaming", "stream_window": 5, "fail_fast": True, "max_diff_rows": 10,
    })["result"]["summary"]
    assert out["stopped_early"] is True
    assert out["value_difference_rows"] >= 10          # at least the tripwire
    assert out["value_difference_rows"] < 100          # but not the whole table


def test_progress_callback_writes_pct(client):
    """The progress callback publishes {pct, processed, total} on the job while a run is in flight."""
    src, dst = _conn(client, "pg-src", SRC), _conn(client, "pg-dst", SRC)
    jid = client.post(f"{API}/compares", json={
        "kind": "data", "name": "pg",
        "source": {"connection_id": src, "table": "ORDERS"},
        "target": {"connection_id": dst, "table": "ORDERS"},
        "options": {"key": ["ORDER_ID"]},
    }).json()["data"]["id"]

    container = client.app.state.container
    local = client.app.state.local_tenant
    from dvp.services.tenancy import resolve_context

    ctx = resolve_context(container.metadata, user_id=local.user_id, org_id=local.org_id, group_id=local.group_id)
    svc = CompareService(container.metadata, container.results, container.secrets, container.spark)
    cb = svc._progress_cb(ctx, jid)
    cb(500, 1000)
    with container.metadata.session(None) as s:
        prog = s.get(CompareJob, jid).progress
    assert prog == {"pct": 50, "processed": 500, "total": 1000}


def test_window_sizing_respects_budget():
    # No budget → default batch; a tiny budget shrinks the window (clamped to a sane floor).
    assert _stream_window_rows(None, 20) == 50_000
    small = _stream_window_rows(0.05, 50)
    assert 10_000 <= small <= 500_000
