"""A scheduled check reads MAX(watermark) and a cheap count. A full profile is separate."""

from __future__ import annotations

from dvp.connectors.base import TableRef
from dvp.connectors.inline import InlineConnector
from dvp.observability.observe import observe_check

API = "/api/v1"


def test_inline_check_uses_the_catalog_count_and_does_not_scan():
    connector = InlineConnector(
        {
            "database": "RAW",
            "schema": "PUBLIC",
            "tables": {
                "ORDERS": {
                    "columns": [{"name": "LOAD_TS", "data_type": "TIMESTAMP_NTZ"}],
                    "row_count": 5000,
                    "watermarks": {"LOAD_TS": "2026-10-16T06:00:00"},
                }
            },
        }
    )
    obs = observe_check(connector, TableRef("RAW", "PUBLIC", "ORDERS"), "LOAD_TS")
    assert obs["query_shape"] == "check"
    assert obs["row_count"] == 5000
    assert obs["last_load_ts"] == "2026-10-16T06:00:00"
    assert obs["queries"] == ["information_schema", "metadata_row_count", "max_watermark"]
    assert "quality_score" not in obs
    assert all("null_rate" not in c for c in obs["columns"])


def test_full_profile_quality_survives_the_next_check(client):
    conn = client.post(
        f"{API}/connections",
        json={
            "source_type": "local",
            "name": "orders",
            "auth_method": "none",
            "coordinates": {
                "database": "RAW",
                "schema": "PUBLIC",
                "tables": {
                    "ORDERS": {
                        "columns": [
                            {"name": "ID", "data_type": "NUMBER"},
                            {"name": "EMAIL", "data_type": "VARCHAR", "nullable": True},
                            {"name": "LOAD_TS", "data_type": "TIMESTAMP_NTZ"},
                        ],
                        "rows": [
                            {"ID": 1, "EMAIL": "a@x.com", "LOAD_TS": "2026-10-16T06:00:00"},
                            {"ID": 2, "EMAIL": None, "LOAD_TS": "2026-10-16T07:00:00"},
                        ],
                    }
                },
            },
        },
    ).json()["data"]["id"]
    mid = client.post(
        f"{API}/monitors",
        json={
            "connection_id": conn,
            "name": "orders",
            "scope": {"database": "RAW", "schema": "PUBLIC", "tables": ["ORDERS"]},
            "watermark_column": "LOAD_TS",
        },
    ).json()["data"]["id"]
    table = "RAW.PUBLIC.ORDERS"
    profiled = client.post(f"{API}/monitors/{mid}/tables/{table}:profile").json()["data"]
    assert profiled["query_shape"] == "full_profile"
    assert "quality" in profiled["pillars_recorded"]

    checked = client.post(f"{API}/monitors/{mid}:run").json()["data"]
    assert checked["query_shape"] == "check"
    assert checked["tables"][0]["queries"] == [
        "information_schema",
        "count_star",
        "max_watermark",
    ]

    cards = client.get(f"{API}/monitors/{mid}/tables/{table}/pillars").json()["data"]
    by = {c["pillar"]: c for c in cards["pillars"]}
    assert cards["query_shape"] == "check"
    assert by["volume"]["headline"] == "2 rows"
    assert by["quality"]["configured"] is True
