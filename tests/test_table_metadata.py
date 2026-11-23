"""Per-table load-pattern catalog: external injection, per-table override, classify, surface."""

from __future__ import annotations

API = "/api/v1"


def test_inject_bulk_from_external_system(client):
    body = {
        "source": "acme_ingestion",
        "items": [
            {"table_ref": "ANALYTICS.PUBLIC.RAW_EVENTS", "load_pattern": "append",
             "watermark_column": "LOAD_TS", "watermark_type": "timestamp"},
            {"table_ref": "ANALYTICS.PUBLIC.DIM_CUSTOMER", "load_pattern": "scd2",
             "effective_from_column": "EFFECTIVE_FROM", "current_flag_column": "IS_CURRENT"},
            {"table_ref": "ANALYTICS.PUBLIC.SFDC_ACCOUNTS", "load_pattern": "full_snapshot"},
        ],
    }
    r = client.put(f"{API}/table-metadata", json=body)
    assert r.status_code == 200
    assert r.json()["data"]["upserted"] == 3

    lst = client.get(f"{API}/table-metadata").json()["data"]
    by = {m["table_ref"]: m for m in lst}
    assert by["ANALYTICS.PUBLIC.RAW_EVENTS"]["load_pattern"] == "append"
    assert by["ANALYTICS.PUBLIC.RAW_EVENTS"]["source"] == "acme_ingestion"
    assert by["ANALYTICS.PUBLIC.DIM_CUSTOMER"]["load_pattern"] == "scd2"
    assert by["ANALYTICS.PUBLIC.DIM_CUSTOMER"]["current_flag_column"] == "IS_CURRENT"


def test_inject_is_idempotent_and_overridable(client):
    item = {"table_ref": "DB.S.T", "load_pattern": "append"}
    client.put(f"{API}/table-metadata", json={"source": "sys", "items": [item]})
    # re-inject with a corrected pattern → upsert, not duplicate
    client.put(f"{API}/table-metadata", json={"source": "sys", "items": [{"table_ref": "DB.S.T", "load_pattern": "cdc", "changed_at_column": "UPDATED_AT"}]})
    lst = [m for m in client.get(f"{API}/table-metadata").json()["data"] if m["table_ref"] == "DB.S.T"]
    assert len(lst) == 1 and lst[0]["load_pattern"] == "cdc" and lst[0]["changed_at_column"] == "UPDATED_AT"


def test_unknown_pattern_rejected(client):
    r = client.put(f"{API}/table-metadata", json={"items": [{"table_ref": "X", "load_pattern": "bogus"}]})
    assert r.status_code >= 400


def test_patterns_catalog(client):
    pats = client.get(f"{API}/table-metadata/patterns").json()["data"]
    assert {"append", "cdc", "full_snapshot", "scd2", "partitioned", "reference"} <= set(pats)


def test_classify_proposes_patterns(client):
    scd2 = client.post(f"{API}/table-metadata/classify", json={
        "columns": [{"name": "CUSTOMER_KEY"}, {"name": "EFFECTIVE_FROM"}, {"name": "IS_CURRENT"}],
    }).json()["data"]
    assert scd2["load_pattern"] == "scd2" and scd2["current_flag_column"] == "IS_CURRENT"

    cdc = client.post(f"{API}/table-metadata/classify", json={
        "columns": [{"name": "ID"}, {"name": "UPDATED_AT"}],
    }).json()["data"]
    assert cdc["load_pattern"] == "cdc" and cdc["watermark_column"] == "UPDATED_AT"

    part = client.post(f"{API}/table-metadata/classify", json={
        "columns": [{"name": "AMOUNT"}, {"name": "DT"}],
    }).json()["data"]
    assert part["load_pattern"] == "partitioned" and part["partition_column"] == "DT"

    ref = client.post(f"{API}/table-metadata/classify", json={
        "columns": [{"name": "CODE"}, {"name": "LABEL"}], "row_count": 42,
    }).json()["data"]
    assert ref["load_pattern"] == "reference"


# ----------------------------- resolution: per-table overrides monitor default -----------------------------
def _monitor(client, load_type="incremental"):
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "inline", "name": "raw", "auth_method": "none", "coordinates": {}},
    ).json()["data"]["id"]
    return client.post(
        f"{API}/monitors",
        json={"connection_id": conn, "name": "m", "scope": {"tables": ["RAW_EVENTS"]}, "load_type": load_type},
    ).json()["data"]["id"]


def test_pillars_use_monitor_default_then_injected_override(client):
    mid = _monitor(client, load_type="full_reload")
    tref = "ANALYTICS.PUBLIC.RAW_EVENTS"
    client.post(f"{API}/monitors/{mid}/tables/{tref}/snapshot", json={"row_count": 100})

    # no per-table record yet → falls back to monitor default (full_reload → full_snapshot)
    p = client.get(f"{API}/monitors/{mid}/tables/{tref}/pillars").json()["data"]
    assert p["load_pattern"] == "full_snapshot"
    assert p["load_pattern_source"] == "monitor_default"

    # ingestion system injects the real per-table pattern → overrides the monitor default
    client.put(f"{API}/table-metadata", json={
        "source": "acme_ingestion",
        "items": [{"table_ref": tref, "load_pattern": "append", "watermark_column": "LOAD_TS"}],
    })
    p2 = client.get(f"{API}/monitors/{mid}/tables/{tref}/pillars").json()["data"]
    assert p2["load_pattern"] == "append"
    assert p2["load_pattern_source"] == "acme_ingestion"

    # and it shows per-table in the table list
    tables = client.get(f"{API}/monitors/{mid}/tables").json()["data"]
    assert tables[0]["load_pattern"] == "append"
