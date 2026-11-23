"""sample-by-key preview: pick N keys from source, fetch those rows from source AND target,
aligned by key — over a REAL local SQL source (no mocks). Covers renamed target columns and a
value that differs between the two sides."""

from __future__ import annotations

API = "/api/v1"

# One connection, two tables. Target renames every column and changes AMT for key 2.
COORDS = {
    "database": "RAW",
    "schema": "PUBLIC",
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
                {"CUST_ID": 2, "FULL_NAME": "bob", "AMT": 999},  # differs from source
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


def test_sample_by_key_aligns_source_and_target_with_renamed_columns(client):
    cid = _conn(client)
    body = {
        "source": {"connection_id": cid, "schema": "PUBLIC", "table": "SRC"},
        "target": {"connection_id": cid, "schema": "PUBLIC", "table": "DST"},
        "key": ["ID"],
        "columns": ["ID", "NAME", "AMT"],
        "column_mappings": {"ID": "CUST_ID", "NAME": "FULL_NAME", "AMT": "AMT"},
        "limit": 20,
    }
    d = client.post(f"{API}/compares/sample-by-key", json=body).json()["data"]

    assert d["sampled"] == 3
    assert d["warnings"] == []
    assert d["columns"] == [
        {"src": "ID", "dst": "CUST_ID"},
        {"src": "NAME", "dst": "FULL_NAME"},
        {"src": "AMT", "dst": "AMT"},
    ]

    by_key = {r["key"]["ID"]: r for r in d["rows"]}
    assert set(by_key) == {1, 2, 3}

    # Every key resolves a row on BOTH sides (target read via its renamed columns).
    for r in by_key.values():
        assert r["source"] is not None and r["target"] is not None

    # Key 1 matches end to end; key 2's AMT differs between source and target.
    assert by_key[1]["source"] == {"ID": 1, "NAME": "alice", "AMT": 10}
    assert by_key[1]["target"] == {"CUST_ID": 1, "FULL_NAME": "alice", "AMT": 10}
    assert by_key[2]["source"]["AMT"] == 20
    assert by_key[2]["target"]["AMT"] == 999


def test_sample_by_key_without_key_returns_independent_samples(client):
    """No key → rows can't be aligned, so each side returns its own ~20-row sample."""
    cid = _conn(client)
    body = {
        "source": {"connection_id": cid, "schema": "PUBLIC", "table": "SRC"},
        "target": {"connection_id": cid, "schema": "PUBLIC", "table": "DST"},
        "key": [],  # none picked yet
        "limit": 20,
    }
    d = client.post(f"{API}/compares/sample-by-key", json=body).json()["data"]
    assert d["aligned"] is False
    assert d["source"]["columns"] == ["ID", "NAME", "AMT"]
    assert d["target"]["columns"] == ["CUST_ID", "FULL_NAME", "AMT"]
    assert len(d["source"]["rows"]) == 3 and len(d["target"]["rows"]) == 3
    assert d["source"]["rows"][0] == [1, "alice", 10]


def test_sample_by_key_looks_up_user_provided_keys(client):
    """User supplies explicit key values → we fetch exactly those, in order, from both sides."""
    cid = _conn(client)
    body = {
        "source": {"connection_id": cid, "schema": "PUBLIC", "table": "SRC"},
        "target": {"connection_id": cid, "schema": "PUBLIC", "table": "DST"},
        "key": ["ID"],
        "key_values": [[3], [1]],  # only these two, in this order
        "columns": ["ID", "NAME", "AMT"],
        "column_mappings": {"ID": "CUST_ID", "NAME": "FULL_NAME", "AMT": "AMT"},
        "limit": 20,
    }
    d = client.post(f"{API}/compares/sample-by-key", json=body).json()["data"]
    assert d["provided"] is True
    assert d["sampled"] == 2
    assert [r["key"]["ID"] for r in d["rows"]] == [3, 1]  # preserves requested order
    assert d["rows"][0]["source"] == {"ID": 3, "NAME": "carol", "AMT": 30}
    assert d["rows"][0]["target"] == {"CUST_ID": 3, "FULL_NAME": "carol", "AMT": 30}


def test_sample_by_key_provided_value_is_type_coerced(client):
    """A numeric key typed as a STRING ("2") still lines up with the NUMBER key column."""
    cid = _conn(client)
    body = {
        "source": {"connection_id": cid, "schema": "PUBLIC", "table": "SRC"},
        "target": {"connection_id": cid, "schema": "PUBLIC", "table": "DST"},
        "key": ["ID"],
        "key_values": [["2"]],  # string, not int
        "columns": ["ID", "AMT"],
        "column_mappings": {"ID": "CUST_ID", "AMT": "AMT"},
    }
    d = client.post(f"{API}/compares/sample-by-key", json=body).json()["data"]
    assert d["sampled"] == 1
    r = d["rows"][0]
    assert r["key"]["ID"] == 2
    assert r["source"]["AMT"] == 20 and r["target"]["AMT"] == 999  # the known difference


def test_sample_by_key_provided_missing_key_warns(client):
    """A requested key absent from both sides comes back as a blank row plus a plain warning."""
    cid = _conn(client)
    body = {
        "source": {"connection_id": cid, "schema": "PUBLIC", "table": "SRC"},
        "target": {"connection_id": cid, "schema": "PUBLIC", "table": "DST"},
        "key": ["ID"],
        "key_values": [[1], [999]],  # 999 does not exist
        "columns": ["ID"],
        "column_mappings": {"ID": "CUST_ID"},
    }
    d = client.post(f"{API}/compares/sample-by-key", json=body).json()["data"]
    by_key = {r["key"]["ID"]: r for r in d["rows"]}
    assert by_key[1]["source"] is not None
    assert by_key[999]["source"] is None and by_key[999]["target"] is None
    assert any("not found" in w for w in d["warnings"])


def test_sample_by_key_provided_blank_values_rejected(client):
    """Every key column must be filled; an all-blank row yields a clear warning, not a crash."""
    cid = _conn(client)
    body = {
        "source": {"connection_id": cid, "schema": "PUBLIC", "table": "SRC"},
        "target": {"connection_id": cid, "schema": "PUBLIC", "table": "DST"},
        "key": ["ID"],
        "key_values": [[""]],
        "columns": ["ID"],
    }
    d = client.post(f"{API}/compares/sample-by-key", json=body).json()["data"]
    assert d["sampled"] == 0 and d["rows"] == []
    assert any("needs a value" in w for w in d["warnings"])


def test_sample_by_key_flags_missing_target_row(client):
    """A key present in source but absent in target comes back with target=None (not an error)."""
    coords = {
        "database": "RAW", "schema": "PUBLIC",
        "tables": {
            "SRC": {"columns": [{"name": "ID", "data_type": "NUMBER"}],
                    "rows": [{"ID": 1}, {"ID": 2}]},
            "DST": {"columns": [{"name": "ID", "data_type": "NUMBER"}],
                    "rows": [{"ID": 1}]},  # key 2 missing
        },
    }
    cid = client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "c2", "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]
    body = {
        "source": {"connection_id": cid, "schema": "PUBLIC", "table": "SRC"},
        "target": {"connection_id": cid, "schema": "PUBLIC", "table": "DST"},
        "key": ["ID"], "columns": ["ID"], "limit": 20,
    }
    d = client.post(f"{API}/compares/sample-by-key", json=body).json()["data"]
    by_key = {r["key"]["ID"]: r for r in d["rows"]}
    assert by_key[1]["target"] is not None
    assert by_key[2]["target"] is None
