"""Find Column Bloom index — unit test the filter, then end-to-end over the real local
source (no mocks): build the index via the API, confirm an indexed search returns the same
answers as a live scan and reports used_index=True."""

from __future__ import annotations

from dvp.services.bloom import BloomFilter

API = "/api/v1"

COORDS = {
    "database": "RAW",
    "schema": "PUBLIC",
    "tables": {
        "CUSTOMERS": {
            "columns": [
                {"name": "ID", "data_type": "NUMBER"},
                {"name": "ZIP", "data_type": "STRING"},
                {"name": "STATE", "data_type": "STRING"},
            ],
            "rows": [
                {"ID": 1, "ZIP": "27519", "STATE": "NC"},
                {"ID": 2, "ZIP": "10001", "STATE": "NY"},
            ],
        },
        "ADDRESSES": {
            "columns": [
                {"name": "ADDR_ID", "data_type": "NUMBER"},
                {"name": "POSTAL", "data_type": "NUMBER"},
                {"name": "REGION", "data_type": "STRING"},
            ],
            "rows": [{"ADDR_ID": 9, "POSTAL": 27519, "REGION": "SC"}],
        },
    },
}


def _conn(client, coords=COORDS):
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "c", "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]


# ---------- Bloom unit ----------
def test_bloom_no_false_negatives_and_merge():
    bf = BloomFilter.sized(100, fp=0.01)
    present = [f"v{i}" for i in range(100)]
    for v in present:
        bf.add(v)
    # every inserted value MUST be reported present (no false negatives)
    assert all(v in bf for v in present)
    # merge another filter of the SAME size → union membership holds
    other = BloomFilter(bf.m, bf.k)
    other.add("extra-value")
    bf.merge(other)
    assert "extra-value" in bf
    assert all(v in bf for v in present)


def test_bloom_roundtrip_bytes():
    bf = BloomFilter.sized(50)
    bf.add("needle")
    restored = BloomFilter(bf.m, bf.k, bf.to_bytes())
    assert "needle" in restored


# ---------- end-to-end ----------
def test_build_index_then_search_uses_it(client):
    cid = _conn(client)
    # build the index
    r = client.post(f"{API}/finder/index:build", json={"connection_id": cid})
    assert r.status_code == 200, r.text
    built = r.json()["data"]
    assert built["tables_indexed"] == 2
    assert built["columns_indexed"] == 6  # 3 + 3 columns

    # status reflects coverage
    st = client.get(f"{API}/finder/index/status?connection_id={cid}").json()["data"]
    assert st["indexed"] is True
    assert st["tables_indexed"] == 2 and st["columns_indexed"] == 6
    assert st["last_scanned_at"] is not None

    # indexed single-value search: same answer as live, and it used the index
    data = client.post(
        f"{API}/finder/search", json={"connection_id": cid, "values": ["27519"], "schema": "PUBLIC"}
    ).json()["data"]
    assert data["used_index"] is True
    by_table = {h["table"]: h for h in data["hits"]}
    assert set(by_table) == {"CUSTOMERS", "ADDRESSES"}
    assert by_table["CUSTOMERS"]["sample"]["rows"][0] == [1, "27519", "NC"]

    # co-occurrence still same-row only
    co = client.post(
        f"{API}/finder/search",
        json={"connection_id": cid, "values": ["27519", "NC"], "schema": "PUBLIC", "match_mode": "co_occurrence"},
    ).json()["data"]
    assert co["used_index"] is True
    assert {h["table"] for h in co["hits"]} == {"CUSTOMERS"}


def test_indexed_and_live_scan_agree(client):
    cid = _conn(client)
    client.post(f"{API}/finder/index:build", json={"connection_id": cid})

    def hits(use_index):
        d = client.post(
            f"{API}/finder/search",
            json={"connection_id": cid, "values": ["NC"], "schema": "PUBLIC", "use_index": use_index},
        ).json()["data"]
        return d["used_index"], {(h["table"], c["column"]) for h in d["hits"] for c in h["columns"]}

    used_auto, cols_auto = hits("auto")
    used_never, cols_never = hits("never")
    assert used_auto is True and used_never is False
    assert cols_auto == cols_never == {("CUSTOMERS", "STATE")}


def test_absent_value_no_hits_with_index(client):
    cid = _conn(client)
    client.post(f"{API}/finder/index:build", json={"connection_id": cid})
    data = client.post(
        f"{API}/finder/search", json={"connection_id": cid, "values": ["nope-not-here"], "schema": "PUBLIC"}
    ).json()["data"]
    assert data["used_index"] is True
    assert data["hits"] == []


def test_incremental_rebuild_preserves_matches(client):
    cid = _conn(client)
    client.post(f"{API}/finder/index:build", json={"connection_id": cid})
    # incremental refresh with a watermark — no new rows, so it's effectively a no-op merge,
    # and existing matches must still be found.
    r = client.post(
        f"{API}/finder/index:build",
        json={"connection_id": cid, "incremental": True, "watermark_column": "ID"},
    )
    assert r.status_code == 200, r.text
    data = client.post(
        f"{API}/finder/search", json={"connection_id": cid, "values": ["27519"], "schema": "PUBLIC"}
    ).json()["data"]
    assert {h["table"] for h in data["hits"]} == {"CUSTOMERS", "ADDRESSES"}


def test_date_columns_are_not_indexed(client):
    """Date/time columns aren't supported — they're skipped at index time (not scanned, not
    matched), and a date-looking search value warns the user."""
    coords = {
        "database": "RAW", "schema": "PUBLIC",
        "tables": {"EVENTS": {
            "columns": [
                {"name": "EVENT_ID", "data_type": "NUMBER"},
                {"name": "CREATED_AT", "data_type": "TIMESTAMP"},
                {"name": "EVENT_DATE", "data_type": "DATE"},
            ],
            "rows": [{"EVENT_ID": 1, "CREATED_AT": "2026-11-10T00:00:00", "EVENT_DATE": "2026-11-10"}],
        }},
    }
    cid = _conn(client, coords)
    built = client.post(f"{API}/finder/index:build", json={"connection_id": cid}).json()["data"]
    assert built["date_columns_skipped"] == 2       # CREATED_AT + EVENT_DATE
    assert built["columns_indexed"] == 1            # only EVENT_ID

    # the date value is not found, and the response warns that dates aren't supported
    r = client.post(
        f"{API}/finder/search", json={"connection_id": cid, "values": ["2026-11-10"], "schema": "PUBLIC"}
    ).json()
    assert r["data"]["hits"] == []
    assert any(w["code"] == "dates_unsupported" for w in r["warnings"])

    # the non-date column still works
    idr = client.post(
        f"{API}/finder/search", json={"connection_id": cid, "values": ["1"], "schema": "PUBLIC"}
    ).json()["data"]
    assert {(h["table"], c["column"]) for h in idr["hits"] for c in h["columns"]} == {("EVENTS", "EVENT_ID")}


def test_binary_and_free_text_columns_are_skipped(client):
    """CLOB/BLOB skipped by type; a large HIGH-cardinality free-text column skipped by the policy —
    with a per-column reason. Each build reports what it ignored and why."""
    coords = {
        "database": "RAW", "schema": "PUBLIC",
        "tables": {"DOCS": {
            "columns": [
                {"name": "DOC_ID", "data_type": "NUMBER"},
                {"name": "CODE", "data_type": "VARCHAR"},
                {"name": "BODY", "data_type": "CLOB"},     # binary/LOB → skipped by type
                {"name": "PAYLOAD", "data_type": "BLOB"},  # binary → skipped by type
                {"name": "NOTES", "data_type": "VARCHAR"},  # distinct long prose → free text, skipped
            ],
            # NOTES is long AND high-cardinality (a distinct ~200-char value per row).
            "rows": [
                {"DOC_ID": i, "CODE": f"C{i:04d}", "BODY": "x", "PAYLOAD": "y",
                 "NOTES": f"note {i} " + "lorem ipsum " * 18}
                for i in range(12)
            ],
        }},
    }
    cid = _conn(client, coords)
    built = client.post(f"{API}/finder/index:build", json={"connection_id": cid}).json()["data"]
    assert built["columns_indexed"] == 2         # DOC_ID + CODE
    assert built["columns_skipped"] == 3         # BODY, PAYLOAD, NOTES
    reasons = {d["column"]: d["reason"] for d in built["skipped_columns"]}
    assert "binary/LOB" in reasons["BODY"] and "binary/LOB" in reasons["PAYLOAD"]
    assert "high-cardinality text" in reasons["NOTES"]

    code = client.post(
        f"{API}/finder/search", json={"connection_id": cid, "values": ["C0003"], "schema": "PUBLIC"}
    ).json()["data"]
    assert {(h["table"], c["column"]) for h in code["hits"] for c in h["columns"]} == {("DOCS", "CODE")}


def test_long_low_cardinality_column_is_kept(client):
    """The key improvement over a length-only rule: a LONG but LOW-cardinality column (e.g. a few
    repeated boilerplate texts) is still indexed — cheap exact set, and searchable."""
    clause_a = ("This Agreement is governed by the laws of the State. " * 4).strip()  # ~210 chars
    clause_b = ("All disputes shall be resolved by binding arbitration. " * 4).strip()
    coords = {
        "database": "RAW", "schema": "PUBLIC",
        "tables": {"CONTRACTS": {
            "columns": [{"name": "ID", "data_type": "NUMBER"}, {"name": "TERMS", "data_type": "VARCHAR"}],
            "rows": [{"ID": i, "TERMS": clause_a if i % 2 else clause_b} for i in range(30)],
        }},
    }
    cid = _conn(client, coords)
    built = client.post(f"{API}/finder/index:build", json={"connection_id": cid}).json()["data"]
    assert built["columns_indexed"] == 2         # ID + TERMS (TERMS kept despite being long)
    assert built["columns_skipped"] == 0
    found = client.post(
        f"{API}/finder/search", json={"connection_id": cid, "values": [clause_a], "schema": "PUBLIC"}
    ).json()["data"]
    assert {(h["table"], c["column"]) for h in found["hits"] for c in h["columns"]} == {("CONTRACTS", "TERMS")}


def test_max_text_len_is_configurable(client):
    """Raising max_text_len keeps a long high-cardinality column the default would skip."""
    coords = {
        "database": "RAW", "schema": "PUBLIC",
        "tables": {"T": {"columns": [{"name": "NOTE", "data_type": "VARCHAR"}],
                         "rows": [{"NOTE": f"entry {i} " + "z" * 150} for i in range(10)]}},
    }
    cid = _conn(client, coords)
    d1 = client.post(f"{API}/finder/index:build", json={"connection_id": cid}).json()["data"]
    assert d1["columns_indexed"] == 0 and d1["columns_skipped"] == 1   # default (100) skips it
    d2 = client.post(
        f"{API}/finder/index:build", json={"connection_id": cid, "max_text_len": 500}
    ).json()["data"]
    assert d2["columns_indexed"] == 1 and d2["columns_skipped"] == 0   # threshold raised → kept


def test_index_schedule_config(client):
    cid = _conn(client)
    r = client.put(
        f"{API}/finder/index/config",
        json={"connection_id": cid, "frequency": "daily", "watermark_column": "ID", "enabled": True},
    )
    assert r.status_code == 200, r.text
    cfg = r.json()["data"]
    assert cfg["frequency"] == "daily" and cfg["enabled"] is True
    assert cfg["next_run_at"] is not None
    # status surfaces the config
    st = client.get(f"{API}/finder/index/status?connection_id={cid}").json()["data"]
    assert st["config"]["watermark_column"] == "ID"
