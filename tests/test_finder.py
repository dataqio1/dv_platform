"""Find Column (value → table/column locator) — end-to-end over the real local SQL
source (no mocks): create a connection with seeded rows, hit the HTTP API, assert on
the confirmed hits + sample rows."""

from __future__ import annotations

API = "/api/v1"

# Two tables. CUSTOMERS has a row where zip 27519 and state NC share a row; ADDRESSES has
# 27519 but a DIFFERENT state on that row — so co-occurrence of (27519, NC) must match
# CUSTOMERS only, while a single-value search for 27519 matches both tables.
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
            "rows": [
                {"ADDR_ID": 9, "POSTAL": 27519, "REGION": "SC"},  # same zip, different state
            ],
        },
    },
}


def _conn(client, coords=COORDS):
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "c", "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]


def _search(client, cid, **body):
    body["connection_id"] = cid
    r = client.post(f"{API}/finder/search", json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_single_value_finds_all_tables_and_columns(client):
    cid = _conn(client)
    data = _search(client, cid, values=["27519"], schema="PUBLIC")

    by_table = {h["table"]: h for h in data["hits"]}
    assert set(by_table) == {"CUSTOMERS", "ADDRESSES"}  # found in both (text vs number)

    # CUSTOMERS.ZIP (stored as string) matched
    cust_cols = {(c["column"], c["value"]) for c in by_table["CUSTOMERS"]["columns"]}
    assert ("ZIP", "27519") in cust_cols
    # ADDRESSES.POSTAL (stored as number) matched too — text-cast comparison works across types
    addr_cols = {(c["column"], c["value"]) for c in by_table["ADDRESSES"]["columns"]}
    assert ("POSTAL", "27519") in addr_cols

    # the hit carries confirmed sample rows
    assert by_table["CUSTOMERS"]["match_count"] == 1
    assert by_table["CUSTOMERS"]["sample"]["columns"] == ["ID", "ZIP", "STATE"]
    assert by_table["CUSTOMERS"]["sample"]["rows"][0] == [1, "27519", "NC"]


def test_co_occurrence_requires_same_row(client):
    cid = _conn(client)
    data = _search(client, cid, values=["27519", "NC"], schema="PUBLIC", match_mode="co_occurrence")

    assert data["match_mode"] == "co_occurrence"
    tables = {h["table"] for h in data["hits"]}
    # Only CUSTOMERS has 27519 AND NC in the same row. ADDRESSES has 27519 but region SC.
    assert tables == {"CUSTOMERS"}
    hit = data["hits"][0]
    assert hit["match_count"] == 1
    vals_found = {c["value"] for c in hit["columns"]}
    assert vals_found == {"27519", "NC"}
    assert hit["sample"]["rows"][0] == [1, "27519", "NC"]


def test_no_match_returns_empty(client):
    cid = _conn(client)
    data = _search(client, cid, values=["does-not-exist"], schema="PUBLIC")
    assert data["hits"] == []
    assert data["scanned_tables"] == 2  # it really scanned both tables


def test_empty_value_is_rejected(client):
    cid = _conn(client)
    r = client.post(f"{API}/finder/search", json={"connection_id": cid, "values": ["   "]})
    assert r.status_code == 400 or r.status_code == 422


def test_bad_connection_is_rejected(client):
    r = client.post(f"{API}/finder/search", json={"connection_id": "nope", "values": ["x"]})
    assert r.status_code == 422  # ValidationError("connection not found"), per CatalogService convention


def test_searches_all_connections_when_scope_omitted(client):
    """Omit connection_id entirely → every readable connection is scanned."""
    cid_a = _conn(client)  # has CUSTOMERS + ADDRESSES
    other = {
        "database": "WH",
        "schema": "DW",
        "tables": {
            "SHIPMENTS": {
                "columns": [{"name": "SID", "data_type": "NUMBER"}, {"name": "ZIPCODE", "data_type": "STRING"}],
                "rows": [{"SID": 5, "ZIPCODE": "27519"}],
            },
        },
    }
    cid_b = _conn(client, other)

    # No connection_id and no schema → search everything for 27519.
    r = client.post(f"{API}/finder/search", json={"values": ["27519"]})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    _ = (cid_a, cid_b)
    conns = {h["connection_id"] for h in data["hits"]}
    tables = {h["table"] for h in data["hits"]}
    assert data["scanned_connections"] == 2
    assert conns == {cid_a, cid_b}  # found in both connections
    assert "SHIPMENTS" in tables and "CUSTOMERS" in tables and "ADDRESSES" in tables


def test_broad_scope_emits_a_warning(client):
    """Omitting the schema (all-schemas scan) nudges the caller with a broad_scope warning,
    while pinning a schema does not."""
    cid = _conn(client)
    r = client.post(f"{API}/finder/search", json={"connection_id": cid, "values": ["27519"]})
    assert r.status_code == 200
    assert any(w["code"] == "broad_scope" for w in r.json()["warnings"])

    pinned = client.post(
        f"{API}/finder/search", json={"connection_id": cid, "values": ["27519"], "schema": "PUBLIC"}
    )
    assert not any(w["code"] == "broad_scope" for w in pinned.json()["warnings"])


def test_search_warms_the_table_cache(client):
    """The search's internal table listing goes through the shared catalog cache, so a
    subsequent table-list request is served from cache (no repeat source introspection)."""
    cid = _conn(client)
    _search(client, cid, values=["27519"], schema="PUBLIC")
    cached = client.get(f"{API}/connections/{cid}/tables?schema=PUBLIC").json()["data"]
    assert cached["cached"] is True  # populated by the search


def test_searches_all_schemas_when_schema_omitted(client):
    coords = {
        "database": "RAW",
        "schema": "PUBLIC",
        "tables": {
            "T": {"columns": [{"name": "C", "data_type": "STRING"}], "rows": [{"C": "needle"}]},
        },
    }
    cid = _conn(client, coords)
    # connection pinned, schema omitted → the connector's schema(s) are discovered.
    data = _search(client, cid, values=["needle"])
    assert {h["table"] for h in data["hits"]} == {"T"}


def test_search_reports_columns_scanned(client):
    cid = _conn(client)
    data = _search(client, cid, values=["27519"], schema="PUBLIC")
    # CUSTOMERS has 3 columns, ADDRESSES has 3 → both scanned = 6 columns examined.
    assert data["stats"]["scanned_columns"] == 6
    assert data["scanned_tables"] == 2


def test_search_is_saved_to_history_with_metadata(client):
    cid = _conn(client)
    data = _search(client, cid, values=["27519"], schema="PUBLIC")
    assert data["search_id"]

    hist = client.get(f"{API}/finder/searches").json()["data"]
    assert len(hist) == 1
    s = hist[0]
    assert s["id"] == data["search_id"]
    assert s["values"] == ["27519"]
    assert s["scanned_tables"] == 2
    assert s["scanned_columns"] == 6
    assert s["matched_tables"] == 2
    assert s["total_matches"] >= 2  # 27519 appears in both tables
    assert s["took_ms"] >= 0
    assert s["scope_label"] and "PUBLIC" in s["scope_label"]
    assert s["created_at"]


def test_history_is_most_recent_first(client):
    cid = _conn(client)
    _search(client, cid, values=["27519"], schema="PUBLIC")
    _search(client, cid, values=["10001"], schema="PUBLIC")
    hist = client.get(f"{API}/finder/searches").json()["data"]
    assert [s["values"] for s in hist] == [["10001"], ["27519"]]


def test_reopen_search_returns_full_stored_result(client):
    cid = _conn(client)
    data = _search(client, cid, values=["27519"], schema="PUBLIC")
    sid = data["search_id"]

    reopened = client.get(f"{API}/finder/searches/{sid}").json()["data"]
    assert reopened["id"] == sid
    # Full payload (hits + sample rows) is stored, so re-open needs no re-scan.
    assert {h["table"] for h in reopened["result"]["hits"]} == {"CUSTOMERS", "ADDRESSES"}


def test_delete_search_removes_it(client):
    cid = _conn(client)
    data = _search(client, cid, values=["27519"], schema="PUBLIC")
    sid = data["search_id"]

    assert client.delete(f"{API}/finder/searches/{sid}").status_code == 200
    assert client.get(f"{API}/finder/searches").json()["data"] == []
    # A deleted (or unknown) search 404s on re-open.
    assert client.get(f"{API}/finder/searches/{sid}").status_code == 404
