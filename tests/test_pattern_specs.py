"""Pattern-spec catalog + columns endpoint (drive the per-table load-pattern UI)."""

from __future__ import annotations

from dvp.services.table_metadata import LOAD_PATTERNS, pattern_specs

API = "/api/v1"


def test_pattern_specs_cover_every_load_pattern():
    specs = pattern_specs()
    patterns = {p["pattern"] for p in specs["patterns"]}
    # Every validated load pattern must have a UI spec (and vice-versa) so the picker and
    # the persistence layer never disagree.
    assert patterns == set(LOAD_PATTERNS)
    # Every field a pattern references must have an input descriptor.
    for p in specs["patterns"]:
        for f in p["fields"]:
            assert f in specs["fields"], f"missing field spec for {f}"


def test_pattern_specs_endpoint(client):
    data = client.get(f"{API}/table-metadata/pattern-specs").json()["data"]
    labels = {p["pattern"]: p["label"] for p in data["patterns"]}
    assert labels["full_snapshot"].lower().startswith("truncate")
    assert labels["scd2"].startswith("SCD Type 2")
    assert "watermark_column" in data["fields"]


def test_columns_endpoint_on_inline_source(client):
    coords = {
        "database": "RAW_SALES",
        "schema": "PUBLIC",
        "tables": {
            "ORDERS": {
                "columns": [
                    {"name": "ID", "data_type": "NUMBER", "nullable": False},
                    {"name": "LOAD_TS", "data_type": "TIMESTAMP_NTZ", "nullable": True},
                ],
                "rows": [{"ID": 1, "LOAD_TS": "2026-01-01T00:00:00"}],
            },
        },
    }
    cid = client.post(
        f"{API}/connections",
        json={"source_type": "inline", "name": "c", "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]
    cols = client.get(
        f"{API}/connections/{cid}/columns?schema=PUBLIC&table=ORDERS"
    ).json()["data"]["columns"]
    names = {c["name"] for c in cols}
    assert names == {"ID", "LOAD_TS"}
    load_ts = next(c for c in cols if c["name"] == "LOAD_TS")
    assert load_ts["nullable"] is True
