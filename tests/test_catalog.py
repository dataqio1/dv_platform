"""Catalog browsing: list schemas, then tables/views in a schema (no mocks)."""

from __future__ import annotations

API = "/api/v1"

COORDS = {
    "database": "RAW_SALES",
    "schema": "PUBLIC",
    "tables": {
        "ORDERS": {"columns": [{"name": "ID", "data_type": "NUMBER"}], "rows": [{"ID": 1}]},
        "CUSTOMERS": {"columns": [{"name": "ID", "data_type": "NUMBER"}], "rows": [{"ID": 1}]},
    },
}


def _conn(client, source_type="local", coords=COORDS):
    return client.post(
        f"{API}/connections",
        json={
            "source_type": source_type,
            "name": "c",
            "auth_method": "none",
            "coordinates": coords,
        },
    ).json()["data"]["id"]


def test_list_schemas_then_tables(client):
    cid = _conn(client)
    schemas = client.get(f"{API}/connections/{cid}/schemas").json()["data"]["schemas"]
    assert schemas == ["PUBLIC"]

    tables = client.get(f"{API}/connections/{cid}/tables?schema=PUBLIC").json()["data"]["tables"]
    names = {t["name"] for t in tables}
    assert names == {"ORDERS", "CUSTOMERS"}
    assert all(t["type"] == "table" for t in tables)


def test_catalog_on_inline_source(client):
    cid = _conn(client, "inline", COORDS)
    schemas = client.get(f"{API}/connections/{cid}/schemas").json()["data"]["schemas"]
    assert schemas == ["PUBLIC"]
    tables = client.get(f"{API}/connections/{cid}/tables?schema=PUBLIC").json()["data"]["tables"]
    assert {t["name"] for t in tables} == {"ORDERS", "CUSTOMERS"}


def test_sample_rows_and_row_count(client):
    coords = {
        "database": "RAW_SALES",
        "schema": "PUBLIC",
        "tables": {
            "ORDERS": {
                "columns": [{"name": "ID", "data_type": "NUMBER"}, {"name": "AMT", "data_type": "NUMBER"}],
                "rows": [{"ID": 1, "AMT": 10}, {"ID": 2, "AMT": 20}, {"ID": 3, "AMT": 30}],
            },
        },
    }
    cid = _conn(client, "local", coords)

    sample = client.get(f"{API}/connections/{cid}/sample?schema=PUBLIC&table=ORDERS&limit=2").json()["data"]
    assert sample["columns"] == ["ID", "AMT"]
    assert len(sample["rows"]) == 2  # capped by limit
    assert sample["rows"][0] == [1, 10]

    rc = client.get(f"{API}/connections/{cid}/rowcount?schema=PUBLIC&table=ORDERS").json()["data"]
    assert rc["count"] == 3  # exact count for the local source
    assert rc["approx"] is False


def test_sample_bad_table_is_warning_not_500(client):
    cid = _conn(client)
    r = client.get(f"{API}/connections/{cid}/sample?schema=PUBLIC&table=NOPE")
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["rows"] == [] and body["columns"] == []


def test_sql_preview_validates_a_good_query(client):
    """A valid SQL dataset: the endpoint runs it (capped) and returns columns + sample rows."""
    coords = {
        "database": "RAW_SALES",
        "schema": "PUBLIC",
        "tables": {
            "ORDERS": {
                "columns": [{"name": "ID", "data_type": "NUMBER"}, {"name": "AMT", "data_type": "NUMBER"}],
                "rows": [{"ID": 1, "AMT": 10}, {"ID": 2, "AMT": 20}, {"ID": 3, "AMT": 30}],
            },
        },
    }
    cid = _conn(client, "local", coords)
    r = client.post(
        f"{API}/connections/{cid}/sql-preview",
        json={"sql": "SELECT ID, AMT + 1 AS AMT_PLUS FROM ORDERS;", "limit": 2},
    )
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["ok"] is True
    assert body["columns"] == ["ID", "AMT_PLUS"]
    assert len(body["rows"]) == 2  # capped by the preview limit even though the table has 3
    assert body["rows"][0] == [1, 11]  # the computed column really executed


def test_sql_preview_bad_query_is_warning_not_500(client):
    """An invalid query comes back as ok=False with the DB error carried in a warning."""
    cid = _conn(client)
    r = client.post(
        f"{API}/connections/{cid}/sql-preview",
        json={"sql": "SELECT FROM WHERE", "limit": 5},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["ok"] is False
    assert body["data"]["columns"] == [] and body["data"]["rows"] == []
    assert any(w["code"] == "sql_invalid" for w in body["warnings"])


def test_sql_preview_empty_query_is_rejected(client):
    cid = _conn(client)
    r = client.post(f"{API}/connections/{cid}/sql-preview", json={"sql": "   ", "limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["ok"] is False
    assert any(w["code"] == "empty_sql" for w in body["warnings"])


def test_catalog_listing_is_cached_and_refreshable(client):
    """Schema/table listings are cached after the first (slow) fetch and bust on refresh."""
    cid = _conn(client)

    r1 = client.get(f"{API}/connections/{cid}/schemas").json()["data"]
    assert r1["schemas"] == ["PUBLIC"]
    assert r1["cached"] is False and r1["cached_at"]  # first fetch populates the cache
    r2 = client.get(f"{API}/connections/{cid}/schemas").json()["data"]
    assert r2["cached"] is True  # served from cache
    assert r2["cached_at"] == r1["cached_at"]

    t1 = client.get(f"{API}/connections/{cid}/tables?schema=PUBLIC").json()["data"]
    assert t1["cached"] is False
    t2 = client.get(f"{API}/connections/{cid}/tables?schema=PUBLIC").json()["data"]
    assert t2["cached"] is True

    # the refresh endpoint clears everything and re-fetches schemas fresh
    rf = client.post(f"{API}/connections/{cid}/catalog:refresh").json()["data"]
    assert rf["cached"] is False and rf["schemas"] == ["PUBLIC"]
    assert rf["cleared"] >= 1
    # tables were cleared too → next read is a fresh fetch again
    assert client.get(f"{API}/connections/{cid}/tables?schema=PUBLIC").json()["data"]["cached"] is False

    # explicit refresh=true also bypasses the cache
    assert client.get(f"{API}/connections/{cid}/schemas?refresh=true").json()["data"]["cached"] is False


def test_schema_defaults_to_main_when_unspecified(client):
    cid = _conn(
        client,
        "local",
        {"tables": {"T": {"columns": [{"name": "ID", "data_type": "NUMBER"}], "rows": []}}},
    )
    schemas = client.get(f"{API}/connections/{cid}/schemas").json()["data"]["schemas"]
    assert schemas == ["main"]
