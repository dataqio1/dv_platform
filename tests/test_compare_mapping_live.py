"""Live data compare across tables whose column names DIFFER (no mocks).

Real local SQLite connectors → auto column mapping (exact/prefix/abbrev) aligns the sides,
and the compare reports under SOURCE names. Also covers the /suggest-mapping preview.
"""

from __future__ import annotations

API = "/api/v1"


def _cols(*specs):
    return [{"name": n, "data_type": t, "nullable": True} for n, t in specs]


# Source uses canonical names; target uses ETL-renamed columns:
#   ORDER_ID → o_orderkey (prefix), AMOUNT → amt (abbrev), STATUS → status (exact, lower)
SRC = {
    "columns": _cols(("ORDER_ID", "NUMBER"), ("AMOUNT", "NUMBER"), ("STATUS", "VARCHAR")),
    "rows": [
        {"ORDER_ID": 1, "AMOUNT": 10, "STATUS": "OPEN"},
        {"ORDER_ID": 2, "AMOUNT": 20, "STATUS": "OPEN"},
        {"ORDER_ID": 3, "AMOUNT": 30, "STATUS": "CLOSED"},
    ],
}
DST = {
    "columns": _cols(("o_orderkey", "NUMBER"), ("amt", "DECIMAL"), ("status", "VARCHAR")),
    "rows": [
        {"o_orderkey": 1, "amt": 10, "status": "OPEN"},
        {"o_orderkey": 2, "amt": 25, "status": "OPEN"},  # amt differs
        {"o_orderkey": 4, "amt": 40, "status": "NEW"},  # target-only
    ],
}


def _conn(client, name, table, spec):
    coords = {"database": "DB", "schema": "PUBLIC", "tables": {table: spec}}
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": name, "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]


def test_suggest_mapping_fuzzy_is_names_only(client):
    """Default (fuzzy) mode matches on names only — it does NOT sample data, so a renamed column
    with no name hint (ORDER_ID↔o_orderkey) stays unmapped."""
    s = _conn(client, "sf", "ORDERS", SRC)
    t = _conn(client, "tf", "ORDERS_DIM", DST)
    body = {
        "source": {"connection_id": s, "table": "ORDERS"},
        "target": {"connection_id": t, "table": "ORDERS_DIM"},
    }
    r = client.post(f"{API}/compares/suggest-mapping", json=body).json()["data"]
    assert r["mode"] == "fuzzy"
    mapped = {m["source"]: m["target"] for m in r["mappings"]}
    assert mapped.get("AMOUNT") == "amt" and mapped.get("STATUS") == "status"
    assert "ORDER_ID" not in mapped  # no name hint, and fuzzy doesn't look at data
    assert "ORDER_ID" in r["unmapped_source"]


def test_suggest_mapping_deep_matches_by_data(client):
    """Deep mode samples data and lines up renamed columns by value overlap, with a probability."""
    s = _conn(client, "s", "ORDERS", SRC)
    t = _conn(client, "t", "ORDERS_DIM", DST)
    body = {
        "source": {"connection_id": s, "table": "ORDERS"},
        "target": {"connection_id": t, "table": "ORDERS_DIM"},
        "mode": "deep",
    }
    r = client.post(f"{API}/compares/suggest-mapping", json=body).json()["data"]
    assert r["mode"] == "deep"
    mapped = {m["source"]: m["target"] for m in r["mappings"]}
    assert mapped["ORDER_ID"] == "o_orderkey"
    assert mapped["AMOUNT"] == "amt"
    assert mapped["STATUS"] == "status"
    methods = {m["source"]: m["method"] for m in r["mappings"]}
    # STATUS/AMOUNT resolve by name; ORDER_ID has no name hint → value-similarity fallback
    assert methods["STATUS"] == "EXACT_MATCH"
    assert methods["AMOUNT"] == "ABBREVIATION_MATCH"
    assert methods["ORDER_ID"] == "DATA_SIMILARITY"
    # the data match carries a probability the user confirms
    assert 0 < next(m["confidence"] for m in r["mappings"] if m["source"] == "ORDER_ID") <= 1
    assert not r["unmapped_source"]


def test_suggest_mapping_skips_key_detection_when_opted_out(client):
    """detect_key=false maps columns (cheap) but does NOT sample data to find keys — the UI
    uses this on expand so it stays fast; the user asks for keys separately."""
    s = _conn(client, "s_nokey", "ORDERS", SRC)
    t = _conn(client, "t_nokey", "ORDERS_DIM", DST)
    body = {
        "source": {"connection_id": s, "table": "ORDERS"},
        "target": {"connection_id": t, "table": "ORDERS_DIM"},
        "detect_key": False,
    }
    r = client.post(f"{API}/compares/suggest-mapping", json=body).json()["data"]
    assert len(r["mappings"]) == 2  # fuzzy maps AMOUNT/STATUS by name; keys are a separate step
    assert r["source_key"] == [] and r["target_key"] == []
    assert r["key_method"] == "none"


def test_detect_keys_endpoint(client):
    """The on-demand key detector finds the unique key and maps it to the target names."""
    s = _conn(client, "s_dk", "ORDERS", SRC)
    t = _conn(client, "t_dk", "ORDERS_DIM", DST)
    body = {
        "source": {"connection_id": s, "table": "ORDERS"},
        "target": {"connection_id": t, "table": "ORDERS_DIM"},
    }
    r = client.post(f"{API}/compares/detect-keys", json=body).json()["data"]
    assert r["source_key"] == ["ORDER_ID"]
    assert r["target_key"] == ["o_orderkey"]
    assert r["key_method"] == "detected"
    assert r["key_uniqueness_pct"] == 100.0


def test_detect_keys_without_target(client):
    """A target is optional — detecting keys for a single dataset returns source==target key."""
    s = _conn(client, "s_dk1", "ORDERS", SRC)
    r = client.post(
        f"{API}/compares/detect-keys",
        json={"source": {"connection_id": s, "table": "ORDERS"}},
    ).json()["data"]
    assert r["source_key"] == ["ORDER_ID"]
    assert r["target_key"] == ["ORDER_ID"]


def test_run_history_grows_with_each_run_and_is_deleted_with_the_job(client):
    """Each execution is recorded as its own history entry (so the Executions view grows per run),
    and a test case's run history is removed when the test case is deleted."""
    s = _conn(client, "sh", "ORDERS", SRC)
    t = _conn(client, "th", "ORDERS_DIM", DST)
    job = client.post(f"{API}/compares", json={
        "kind": "data", "name": "history test",
        "source": {"connection_id": s, "table": "ORDERS"},
        "target": {"connection_id": t, "table": "ORDERS_DIM"},
        "options": {},
    }).json()["data"]

    for _ in range(3):
        assert client.post(f"{API}/compares/{job['id']}:run").json()["data"]["status"] == "succeeded"

    runs = client.get(f"{API}/compares/runs").json()["data"]
    mine = [e for e in runs if e["type"] == "single" and e["run"]["job_id"] == job["id"]]
    assert len(mine) == 3  # three runs → three history entries (not one overwritten row)
    assert all(e["run"]["status"] == "succeeded" for e in mine)

    client.delete(f"{API}/compares/{job['id']}")
    runs2 = client.get(f"{API}/compares/runs").json()["data"]
    assert not [e for e in runs2 if e["type"] == "single" and e["run"]["job_id"] == job["id"]]


def test_batch_run_history_groups_pairs(client):
    """A batch execution's pair-runs collapse into ONE history entry via their shared batch run."""
    coords = {"database": "DB", "schema": "PUBLIC", "tables": {"ORDERS": SRC, "ITEMS": SRC}}
    c = client.post(f"{API}/connections", json={
        "source_type": "local", "name": "bh", "auth_method": "none", "coordinates": coords,
    }).json()["data"]["id"]
    r = client.post(f"{API}/compares/batch", json={
        "kind": "data", "name": "nightly",
        "source": {"connection_id": c, "schema": "PUBLIC"},
        "target": {"connection_id": c, "schema": "PUBLIC"},
        "tables": ["ORDERS", "ITEMS"],
        "options": {"key": ["ORDER_ID"]},
    })
    assert r.status_code == 201
    runs = client.get(f"{API}/compares/runs").json()["data"]
    batches = [e for e in runs if e["type"] == "batch"]
    assert len(batches) == 1 and batches[0]["total"] == 2  # two pairs, one grouped entry


def test_per_table_counts_conditions_override_batch_default(client):
    """Each table pair in a counts batch can carry its OWN pass condition. Batch default here is
    EXACT; one pair sets a per-table row tolerance so its otherwise-failing count passes, while a
    pair with no override still fails under the exact default — proving the per-table override."""
    def tbl(n):
        return {"columns": _cols(("ID", "NUMBER")), "rows": [{"ID": i} for i in range(n)]}

    def conn(name, orders_n, items_n, parts_n):
        return client.post(f"{API}/connections", json={
            "source_type": "local", "name": name, "auth_method": "none",
            "coordinates": {"database": "DB", "schema": "PUBLIC",
                            "tables": {"ORDERS": tbl(orders_n), "ITEMS": tbl(items_n), "PARTS": tbl(parts_n)}},
        }).json()["data"]["id"]

    sc = conn("csrc", 10, 10, 10)
    dc = conn("cdst", 10, 8, 7)  # ITEMS differs by 2, PARTS by 3

    def pair(t, opts=None):
        p = {"source": {"connection_id": sc, "table": t}, "target": {"connection_id": dc, "table": t}, "label": t}
        if opts:
            p["options"] = opts
        return p

    r = client.post(f"{API}/compares/batch", json={
        "kind": "count", "name": "counts",
        "source": {"connection_id": sc, "schema": "PUBLIC"},
        "target": {"connection_id": dc, "schema": "PUBLIC"},
        "table_pairs": [pair("ORDERS"), pair("ITEMS", {"tolerance_rows": 5}), pair("PARTS")],
        "options": {},  # batch default = exact match
    })
    assert r.status_code == 201
    by = {j["name"]: (j.get("summary") or (j.get("result") or {}).get("summary")) for j in r.json()["data"]["jobs"]}
    assert by["counts: ORDERS"]["match"] is True                      # exact 10 == 10
    assert by["counts: ITEMS"]["match"] is True and by["counts: ITEMS"]["diff"] == 2  # per-table tol 5 ≥ 2
    assert by["counts: PARTS"]["match"] is False and by["counts: PARTS"]["diff"] == 3  # no override → exact fails


def test_memory_budget_guards_the_in_memory_engine(client):
    """When the IN-MEMORY engine is used and the compare would exceed its memory budget, it fails
    with a clear, actionable message BEFORE loading rows — rather than risking an OOM. A generous
    budget runs fine. (A numeric key would instead auto-stream; here we force in-memory to exercise
    the guard — see test_over_budget_numeric_key_auto_streams_instead_of_failing.)"""
    s = _conn(client, "sm", "ORDERS", SRC)
    t = _conn(client, "tm", "ORDERS", SRC)
    body = lambda mem, engine=None: {  # noqa: E731
        "kind": "data", "name": "mem",
        "source": {"connection_id": s, "table": "ORDERS"},
        "target": {"connection_id": t, "table": "ORDERS"},
        "options": {"key": ["ORDER_ID"], "memory_gb": mem, **({"engine": engine} if engine else {})},
    }
    # Absurdly small budget + forced in-memory → the estimate exceeds it → failed with the message.
    job = client.post(f"{API}/compares", json=body(1e-9, "inmemory")).json()["data"]
    res = client.post(f"{API}/compares/{job['id']}:run").json()["data"]
    assert res["status"] == "failed"
    assert "memory budget" in (res["error"] or "").lower()

    # A generous budget → the compare runs normally.
    job2 = client.post(f"{API}/compares", json=body(8)).json()["data"]
    res2 = client.post(f"{API}/compares/{job2['id']}:run").json()["data"]
    assert res2["status"] == "succeeded"


def test_over_budget_numeric_key_auto_routes_to_bounded_engine(client):
    """With a single numeric key, a compare that would blow the in-memory budget AUTO-routes to a
    bounded engine (fingerprint — preferred for its in-DB pruning — or streaming) and succeeds,
    instead of failing. No manual intervention."""
    s = _conn(client, "ss", "ORDERS", SRC)
    t = _conn(client, "ts", "ORDERS", SRC)
    job = client.post(f"{API}/compares", json={
        "kind": "data", "name": "autostream",
        "source": {"connection_id": s, "table": "ORDERS"},
        "target": {"connection_id": t, "table": "ORDERS"},
        "options": {"key": ["ORDER_ID"], "memory_gb": 1e-9},  # tiny budget, numeric key, auto engine
    }).json()["data"]
    res = client.post(f"{API}/compares/{job['id']}:run").json()["data"]
    assert res["status"] == "succeeded"
    assert res["result"]["summary"]["engine"] in ("fingerprint", "streaming")

    # Forcing streaming still works (the guaranteed-correct floor, any dialect/type).
    job2 = client.post(f"{API}/compares", json={
        "kind": "data", "name": "forcestream",
        "source": {"connection_id": s, "table": "ORDERS"},
        "target": {"connection_id": t, "table": "ORDERS"},
        "options": {"key": ["ORDER_ID"], "engine": "streaming"},
    }).json()["data"]
    res2 = client.post(f"{API}/compares/{job2['id']}:run").json()["data"]
    assert res2["status"] == "succeeded" and res2["result"]["summary"]["engine"] == "streaming"


def test_data_compare_with_different_names(client):
    s = _conn(client, "s2", "ORDERS", SRC)
    t = _conn(client, "t2", "ORDERS_DIM", DST)
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "data",
            "name": "renamed compare",
            "source": {"connection_id": s, "table": "ORDERS"},
            "target": {"connection_id": t, "table": "ORDERS_DIM"},
            "options": {},  # auto key + auto column mapping
        },
    ).json()["data"]
    res = client.post(f"{API}/compares/{job['id']}:run").json()["data"]
    assert res["status"] == "succeeded"
    s_ = res["result"]["summary"]
    assert s_["matched_rows"] == 1  # ORDER_ID=1
    assert s_["value_difference_rows"] == 1  # ORDER_ID=2 (amt 20≠25)
    assert s_["source_only_rows"] == 1  # ORDER_ID=3
    assert s_["dst_only_rows"] == 1  # ORDER_ID=4

    # drill-down reports cells under SOURCE names
    diffs = client.get(
        f"{API}/compares/{job['id']}/differences?bucket=value_difference"
    ).json()["data"]
    assert diffs[0]["key"] == {"ORDER_ID": 2}
    cols = {c["column"] for c in diffs[0]["cells"]}
    assert {"AMOUNT", "STATUS"} <= cols


def test_explicit_mapping_overrides(client):
    s = _conn(client, "s3", "ORDERS", SRC)
    t = _conn(client, "t3", "ORDERS_DIM", DST)
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "data",
            "name": "explicit map",
            "source": {"connection_id": s, "table": "ORDERS"},
            "target": {"connection_id": t, "table": "ORDERS_DIM"},
            "options": {
                "key": ["ORDER_ID"],
                "target_key": ["o_orderkey"],
                "column_mappings": {"AMOUNT": "amt", "STATUS": "status"},
            },
        },
    ).json()["data"]
    res = client.post(f"{API}/compares/{job['id']}:run").json()["data"]
    assert res["status"] == "succeeded"
    assert res["result"]["summary"]["matched_rows"] == 1
