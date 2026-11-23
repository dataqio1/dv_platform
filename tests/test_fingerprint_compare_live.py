"""Cross-database fingerprint fast path: in-DB checksum + bisection prunes matching key-ranges and
pulls only divergent leaves, with results identical to the in-memory engine. Two SEPARATE local
SQLite connectors stand in for two different engines (the per-side MD5 checksum folds to the same
integer on both). Real DB, no mocks."""

from __future__ import annotations

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
        "kind": "data", "name": "fp",
        "source": {"connection_id": src, "table": "ORDERS"},
        "target": {"connection_id": dst, "table": "ORDERS"},
        "options": {"key": ["ORDER_ID"], **options},
    }
    jid = client.post(f"{API}/compares", json=body).json()["data"]["id"]
    client.post(f"{API}/compares/{jid}:run")
    return client.get(f"{API}/compares/{jid}").json()["data"]


def test_fingerprint_identical_tables_move_no_rows(client):
    rows = [{"ORDER_ID": i, "AMOUNT": i * 10} for i in range(1, 501)]
    src, dst = _conn(client, "fp-eq-s", rows), _conn(client, "fp-eq-d", list(rows))
    res = _run(client, src, dst, {"engine": "fingerprint"})
    s = res["result"]["summary"]
    assert s["engine"] == "fingerprint"
    assert s["match"] is True and s["matched_rows"] == 500 and s["keys_total"] == 500
    # The full-table checksum matched → the engine reports it moved no rows.
    notes = " ".join(w["message"] for w in res["result"]["warnings"])
    assert "no rows read" in notes


def test_fingerprint_finds_the_few_differences(client):
    # 500 rows; only 3 differ, 1 missing in target, 1 extra in target → mostly-equal migration check.
    src_rows = [{"ORDER_ID": i, "AMOUNT": i} for i in range(1, 501)]
    dst_rows = [{"ORDER_ID": i, "AMOUNT": i} for i in range(1, 501)]
    for i in (17, 200, 450):              # value differences
        dst_rows[i - 1] = {"ORDER_ID": i, "AMOUNT": -1}
    dst_rows = [r for r in dst_rows if r["ORDER_ID"] != 99]   # 99 missing → source_only
    dst_rows.append({"ORDER_ID": 777, "AMOUNT": 7})           # extra → target_only
    src, dst = _conn(client, "fp-s", src_rows), _conn(client, "fp-d", dst_rows)

    fp = _run(client, src, dst, {"engine": "fingerprint"})["result"]["summary"]
    assert fp["engine"] == "fingerprint"
    assert fp["value_difference_rows"] == 3
    assert fp["source_only_rows"] == 1      # id 99
    assert fp["target_only_rows"] == 1      # id 777
    assert fp["matched_rows"] == 496        # 500 − 3 diffs − 1 missing

    # Parity: the in-memory engine on the same data agrees bucket-for-bucket.
    src2, dst2 = _conn(client, "fp-s2", src_rows), _conn(client, "fp-d2", dst_rows)
    mem = _run(client, src2, dst2, {"engine": "inmemory"})["result"]["summary"]
    for k in ("value_difference_rows", "source_only_rows", "target_only_rows", "matched_rows", "keys_total"):
        assert fp[k] == mem[k], k


def test_fingerprint_prunes_matching_ranges(client, monkeypatch):
    """The matching bulk is pruned in-database; only the small divergent chunk is pulled. A single
    mid-table difference means most rows are pruned and few are read — the whole point of the fast
    path. The engine prunes at the CHUNK level (CHUNK_ROWS), so pruning only kicks in once the table
    spans several chunks; we shrink the chunk size here so a 1,000-row table exercises real
    multi-chunk pruning (patching an internal tuning constant — the SQLite bisection SQL is real,
    nothing is mocked)."""
    monkeypatch.setattr("dvp.testing_engine.fingerprint_compare.CHUNK_ROWS", 50)  # 1000 rows → 20 chunks
    src_rows = [{"ORDER_ID": i, "AMOUNT": i} for i in range(1, 1001)]
    dst_rows = [{"ORDER_ID": i, "AMOUNT": i} for i in range(1, 1001)]
    dst_rows[500] = {"ORDER_ID": 501, "AMOUNT": -1}  # a single difference in the middle
    src, dst = _conn(client, "fp-prune-s", src_rows), _conn(client, "fp-prune-d", dst_rows)
    res = _run(client, src, dst, {"engine": "fingerprint", "leaf_rows": 40})
    s = res["result"]["summary"]
    assert s["value_difference_rows"] == 1 and s["matched_rows"] == 999

    note = next(w["message"] for w in res["result"]["warnings"] if "fingerprint compare" in w["message"])
    import re

    pruned = int(re.search(r"pruned ([\d,]+)", note).group(1).replace(",", ""))
    pulled = int(re.search(r"pulled ([\d,]+)", note).group(1).replace(",", ""))
    # Most of the 1000 rows are pruned in-DB; only the leaf around the one diff (~tens of rows ×2
    # sides) is pulled. Pruning dominates — that's the bandwidth win.
    assert pruned >= 800, note
    assert pulled <= 200, note


def test_fingerprint_auto_selected_for_large_cross_db(client):
    """With a single numeric key and a tiny memory budget, a cross-'engine' compare auto-routes to
    fingerprint (preferred over streaming) without being asked."""
    rows = [{"ORDER_ID": i, "AMOUNT": i} for i in range(1, 51)]
    src, dst = _conn(client, "fp-auto-s", rows), _conn(client, "fp-auto-d", list(rows))
    res = _run(client, src, dst, {"memory_gb": 1e-9})  # auto engine, tiny budget → not in-memory
    assert res["result"]["summary"]["engine"] == "fingerprint"
    assert res["result"]["summary"]["match"] is True
