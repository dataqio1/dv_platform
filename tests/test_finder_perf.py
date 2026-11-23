"""Performance-characteristic tests for Find Column: the pruning cascade must keep the amount
of work (Bloom blobs loaded, tables confirmed) proportional to the SURVIVORS, not the catalog
size — and emit sargable SQL on typed warehouses. These assert the telemetry the search now
returns, which is deterministic (unlike wall-clock)."""

from __future__ import annotations

from dvp.services.finder import _pred

API = "/api/v1"


def _wide_coords(n_rows: int = 1500) -> dict:
    cols = [
        {"name": "ID", "data_type": "NUMBER"},        # unique high-card integer
        {"name": "ORDER_NO", "data_type": "NUMBER"},  # high-card integer
        {"name": "ZIP", "data_type": "STRING"},       # 5-digit code
        {"name": "STATE", "data_type": "STRING"},     # 2-letter low-card
        {"name": "CITY", "data_type": "STRING"},      # lowercase words
        {"name": "EMAIL", "data_type": "STRING"},     # lowercase + @ + .
        {"name": "AMOUNT", "data_type": "NUMBER"},     # decimal
    ]
    states = ["NC", "NY", "CA", "TX", "SC"]
    rows = []
    for i in range(n_rows):
        rows.append({
            "ID": 1_000_000 + i,                 # 7-digit
            "ORDER_NO": 500000 + i,              # 6-digit
            "ZIP": f"{(i * 7) % 100000:05d}",    # 5-digit string
            "STATE": states[i % len(states)],
            "CITY": f"city{'x' * (i % 5)}town",  # lowercase, len >= 8
            "EMAIL": f"user{i}@example.com",
            "AMOUNT": f"{(i % 1000)}.{i % 100:02d}",
        })
    return {"database": "RAW", "schema": "PUBLIC", "tables": {"WIDE": {"columns": cols, "rows": rows}}}


def _conn(client, coords):
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "c", "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]


def _search(client, cid, value, **extra):
    body = {"connection_id": cid, "values": [value], "schema": "PUBLIC", **extra}
    return client.post(f"{API}/finder/search", json=body).json()["data"]


def test_cascade_keeps_work_proportional_to_survivors(client):
    cid = _conn(client, _wide_coords())
    client.post(f"{API}/finder/index:build", json={"connection_id": cid})

    # A 2-letter uppercase value (a state): every numeric/zip/email/city column is pruned by
    # type/charset/length BEFORE any Bloom. STATE is low-card → exact set, so NO Bloom is loaded.
    st = _search(client, cid, "NC")
    assert st["used_index"] is True
    assert st["stats"]["blooms_loaded"] == 0, st["stats"]
    assert st["stats"]["tables_confirmed"] <= 1
    assert {(h["table"], c["column"]) for h in st["hits"] for c in h["columns"]} == {("WIDE", "STATE")}

    # A 7-digit id: only the two high-card integer columns share that shape, so at most a couple
    # of Blooms load — never one per column. ZIP (5-digit) is length-pruned, strings type-pruned.
    idv = _search(client, cid, "1000042")
    assert idv["stats"]["blooms_loaded"] <= 2, idv["stats"]
    assert ("WIDE", "ID") in {(h["table"], c["column"]) for h in idv["hits"] for c in h["columns"]}

    # An impossible value is pruned entirely: zero tables confirmed, zero Blooms, no hits.
    none = _search(client, cid, "no-such-value-anywhere")
    assert none["hits"] == []
    assert none["stats"]["tables_confirmed"] == 0
    assert none["stats"]["blooms_loaded"] == 0
    assert none["stats"]["candidate_tables"] == 0


def test_search_reports_timing_telemetry(client):
    cid = _conn(client, _wide_coords(300))
    client.post(f"{API}/finder/index:build", json={"connection_id": cid})
    stats = _search(client, cid, "NC")["stats"]
    for k in ("took_ms", "shortlist_ms", "confirm_ms", "blooms_loaded", "candidate_tables", "tables_confirmed"):
        assert k in stats
    assert stats["took_ms"] >= 0


def _multi_table_coords(n_tables: int = 6) -> dict:
    tables = {}
    for t in range(n_tables):
        tables[f"T{t}"] = {
            "columns": [{"name": "ID", "data_type": "NUMBER"}, {"name": "STATE", "data_type": "STRING"}],
            "rows": [{"ID": t * 10 + i, "STATE": s} for i, s in enumerate(["NC", "NY", "CA"])],
        }
    return {"database": "RAW", "schema": "PUBLIC", "tables": tables}


def test_parallel_confirm_matches_serial(client):
    """Confirming many candidate tables concurrently must return exactly the serial result."""
    cid = _conn(client, _multi_table_coords())
    client.post(f"{API}/finder/index:build", json={"connection_id": cid})

    def located(concurrency: int) -> set:
        d = _search(client, cid, "NC", confirm_concurrency=concurrency)
        return {(h["table"], c["column"]) for h in d["hits"] for c in h["columns"]}

    serial = located(1)
    parallel = located(4)
    assert serial == parallel
    assert len(serial) == 6  # "NC" is in STATE of all six tables → six candidate confirms
    # all six tables were confirmed (the parallel run really covered them)
    assert _search(client, cid, "NC", confirm_concurrency=4)["stats"]["tables_confirmed"] == 6


def test_build_pushes_down_distinct_not_full_row_scan(client, monkeypatch):
    """The index build must compute in the DB — an aggregate COUNT + a chunked UNION ALL of
    per-column DISTINCT — not pull every row into memory. We spy on the executed SQL."""
    from dvp.connectors.local_sql import LocalSqlConnector

    executed: list[str] = []
    orig = LocalSqlConnector.execute

    def spy(self, sql):  # noqa: ANN001, ANN202
        executed.append(sql)
        return orig(self, sql)

    monkeypatch.setattr(LocalSqlConnector, "execute", spy)

    # Values repeat across many rows — DISTINCT returns few; a full scan would return all.
    coords = {
        "database": "RAW", "schema": "PUBLIC",
        "tables": {"EVENTS": {
            "columns": [{"name": "ID", "data_type": "NUMBER"}, {"name": "STATUS", "data_type": "STRING"}],
            "rows": [{"ID": i, "STATUS": "OPEN" if i % 2 else "CLOSED"} for i in range(40)],
        }},
    }
    cid = _conn(client, coords)
    executed.clear()
    client.post(f"{API}/finder/index:build", json={"connection_id": cid})

    joined = " ".join(executed).upper()
    assert "SELECT DISTINCT" in joined   # pulls distinct values, not every row
    assert "COUNT(*)" in joined          # pushdown aggregate for row/null counts
    assert "UNION ALL" in joined         # BOTH columns' distincts fetched in ONE query
    # the two columns were covered by a single distinct round-trip, not one-query-per-column
    distinct_queries = [q for q in executed if "SELECT DISTINCT" in q.upper()]
    assert len(distinct_queries) == 1
    # and the index is still correct: the repeated value is found with its true row count
    data = _search(client, cid, "OPEN")
    assert data["hits"] and data["hits"][0]["match_count"] == 20


def test_pred_is_sargable_on_warehouses_and_safe_on_sqlite():
    # typed warehouse → native, index-usable predicate (no CAST on the column)
    assert _pred("snowflake", "C", "27519", "numeric") == '"C" = 27519'
    assert _pred("postgres", "C", "NC", "string") == '"C" = \'NC\''
    assert _pred("oracle", "C", "27519", "numeric") == '"C" = 27519'
    # sqlite (untyped affinity) and unknown/date columns → cross-type-safe CAST fallback
    assert "CAST" in _pred("sqlite", "C", "27519", "numeric")
    assert "CAST" in _pred("snowflake", "C", "2026-01-01", None)  # unknown tag → fallback
