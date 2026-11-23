"""Dry-run compare: run the REAL cell compare on a small sample (source keys fetched from both
sides, renamed target columns), WITHOUT persisting. No mocks — real local SQL source."""

from __future__ import annotations

API = "/api/v1"

COORDS = {
    "database": "RAW", "schema": "PUBLIC",
    "tables": {
        "SRC": {
            "columns": [
                {"name": "ID", "data_type": "NUMBER"},
                {"name": "NAME", "data_type": "STRING"},
                {"name": "AMT", "data_type": "NUMBER"},
            ],
            "rows": [
                {"ID": 1, "NAME": "alice", "AMT": 10},
                {"ID": 2, "NAME": "bob", "AMT": 20},
                {"ID": 3, "NAME": "carol", "AMT": 30},
            ],
        },
        "DST": {
            "columns": [
                {"name": "CUST_ID", "data_type": "NUMBER"},
                {"name": "FULL_NAME", "data_type": "STRING"},
                {"name": "AMT", "data_type": "NUMBER"},
            ],
            "rows": [
                {"CUST_ID": 1, "FULL_NAME": "alice", "AMT": 10},
                {"CUST_ID": 2, "FULL_NAME": "bob", "AMT": 999},  # AMT differs for key 2
                {"CUST_ID": 3, "FULL_NAME": "carol", "AMT": 30},
            ],
        },
    },
}


def _conn(client):
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "c", "auth_method": "none", "coordinates": COORDS},
    ).json()["data"]["id"]


def test_dry_run_detects_value_difference_on_sample(client):
    cid = _conn(client)
    body = {
        "source": {"connection_id": cid, "schema": "PUBLIC", "table": "SRC"},
        "target": {"connection_id": cid, "schema": "PUBLIC", "table": "DST"},
        "key": ["ID"],
        "columns": ["ID", "NAME", "AMT"],
        "column_mappings": {"ID": "CUST_ID", "NAME": "FULL_NAME", "AMT": "AMT"},
        "limit": 20,
    }
    d = client.post(f"{API}/compares/dry-run", json=body).json()["data"]

    assert d["sampled"] == 3
    assert d["warnings"] == []
    s = d["summary"]
    assert s["matched_rows"] == 2          # keys 1 & 3 match
    assert s["value_difference_rows"] == 1  # key 2 differs
    assert s["per_column_mismatch"] == {"AMT": 1}
    # The one diff is surfaced as an example row (key 2, AMT 20 vs 999).
    assert any(r.get("bucket") == "value_difference" for r in d["diffs"])


def test_dry_run_all_match_when_identical(client):
    """Dry-run on identical data (renamed cols) reports a clean 100% match — and persists nothing."""
    cid = _conn(client)
    # Compare SRC to itself via SQL that renames columns → logically identical.
    body = {
        "source": {"connection_id": cid, "sql": "SELECT ID, NAME, AMT FROM SRC"},
        "target": {"connection_id": cid, "sql": "SELECT ID AS CUST_ID, NAME AS FULL_NAME, AMT FROM SRC"},
        "key": ["ID"],
        "columns": ["ID", "NAME", "AMT"],
        "column_mappings": {"ID": "CUST_ID", "NAME": "FULL_NAME", "AMT": "AMT"},
        "limit": 20,
    }
    d = client.post(f"{API}/compares/dry-run", json=body).json()["data"]
    assert d["sampled"] == 3
    assert d["summary"]["matched_rows"] == 3
    assert d["summary"]["value_difference_rows"] == 0
    assert d["diffs"] == []

    # Nothing was persisted: no compare jobs exist from the dry run.
    jobs = client.get(f"{API}/compares").json()["data"]
    assert jobs == []
