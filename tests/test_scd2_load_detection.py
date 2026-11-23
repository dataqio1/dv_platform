"""Load-type detection + SCD2-aware DQ rules over the REAL local SQLite connector (no mocks).

Covers docs/design/11: end-date SCD2 classification with prefixed real-world names, the active
predicate, and the two new point-in-time checks (one-active-version-per-key, no-overlap) actually
running as SQL and catching a seeded bug. Pure classifier/predicate/projection checks sit alongside.
"""

from __future__ import annotations

from dvp.connectors.base import TableRef
from dvp.connectors.local_sql import LocalSqlConnector
from dvp.observability.evaluator import evaluate
from dvp.services.table_metadata import (
    classify_pattern,
    project_convention,
    scd2_active_predicate,
)

API = "/api/v1"

# A curated SCD2 dimension using one ETL tool's convention: start/end dates + batch id, with a
# max-future end date (9999-12-31) marking the active version. customer 1 has history (one closed,
# one open); customer 2 is a single open version.
SCD2 = {
    "database": "DW",
    "schema": "GOLD",
    "tables": {
        "CUSTOMER_DIM": {
            "columns": [
                {"name": "CUSTOMER_ID", "data_type": "NUMBER", "nullable": False},
                {"name": "NAME", "data_type": "VARCHAR", "nullable": True},
                {"name": "SRC_EFF_START_DATE", "data_type": "DATE", "nullable": False},
                {"name": "SRC_EFF_END_DATE", "data_type": "DATE", "nullable": False},
                {"name": "BATCH_ID", "data_type": "NUMBER", "nullable": False},
            ],
            "rows": [
                {"CUSTOMER_ID": 1, "NAME": "ACME v1", "SRC_EFF_START_DATE": "2026-01-01",
                 "SRC_EFF_END_DATE": "2026-06-01", "BATCH_ID": 10},
                {"CUSTOMER_ID": 1, "NAME": "ACME v2", "SRC_EFF_START_DATE": "2026-06-01",
                 "SRC_EFF_END_DATE": "9999-12-31", "BATCH_ID": 20},
                {"CUSTOMER_ID": 2, "NAME": "Globex", "SRC_EFF_START_DATE": "2026-02-01",
                 "SRC_EFF_END_DATE": "9999-12-31", "BATCH_ID": 20},
            ],
        }
    },
}
COLS = SCD2["tables"]["CUSTOMER_DIM"]["columns"]


def _meta() -> dict:
    """Resolved per-table metadata for CUSTOMER_DIM (as classify/projection would produce)."""
    return {
        "effective_from_column": "SRC_EFF_START_DATE",
        "effective_to_column": "SRC_EFF_END_DATE",
        "current_flag_column": None,
        "config": {"scd2": {"active_mode": "end_date", "high_date_value": "9999-12-31",
                            "open_end_is_null": True}},
    }


def _tref() -> TableRef:
    return TableRef("DW", "GOLD", "CUSTOMER_DIM")


# ----------------------------- pure: classify / predicate / projection -----------------------------
def test_classify_detects_end_date_scd2_with_prefixed_names():
    r = classify_pattern(COLS)
    assert r["load_pattern"] == "scd2"
    assert r["effective_from_column"] == "SRC_EFF_START_DATE"
    assert r["effective_to_column"] == "SRC_EFF_END_DATE"
    assert r["batch_id_column"] == "BATCH_ID"
    assert r["active_mode"] == "end_date"


def test_active_predicate_end_date():
    pred = scd2_active_predicate(_meta())
    assert '"SRC_EFF_END_DATE" >= \'9999-12-31\'' in pred
    assert "IS NULL" in pred  # open_end_is_null


def test_projection_is_advisory_on_missing_columns():
    conv = {"loaded_at": "dw_load_ts", "effective_to": "SRC_EFF_END_DATE", "batch_id": "BATCH_ID"}
    out = project_convention(conv, COLS)
    assert out["coverage"]["effective_to"] is True
    assert out["coverage"]["loaded_at"] is False  # not present → advisory, not forced
    assert out["fields"]["effective_to_column"] == "SRC_EFF_END_DATE"
    assert "loaded_at" in out["missing_roles"]


# ----------------------------- connector: the checks actually run -----------------------------
def test_active_count_reflects_only_open_rows():
    c = LocalSqlConnector(SCD2)
    pred = scd2_active_predicate(_meta())
    res = c.execute(f'SELECT COUNT(*) FROM "CUSTOMER_DIM" WHERE {pred}')
    assert res.rows[0][0] == 2  # two open versions (cust 1 v2, cust 2) — not the 3 total


def test_scd2_active_unique_passes_when_one_open_per_key():
    c = LocalSqlConnector(SCD2)
    rule = {"type": "scd2_active_unique",
            "config": {"key_columns": ["CUSTOMER_ID"], "active_sql": scd2_active_predicate(_meta())}}
    out = evaluate(c, _tref(), rule)
    assert out.metric_value == 0 and out.passed is True


def test_scd2_active_unique_fails_on_two_open_versions_for_one_key():
    # Break it: customer 1 now has TWO open versions (the load stopped closing the old one).
    broken = {**SCD2, "tables": {"CUSTOMER_DIM": {
        "columns": COLS,
        "rows": [
            {"CUSTOMER_ID": 1, "NAME": "ACME v1", "SRC_EFF_START_DATE": "2026-01-01",
             "SRC_EFF_END_DATE": "9999-12-31", "BATCH_ID": 10},
            {"CUSTOMER_ID": 1, "NAME": "ACME v2", "SRC_EFF_START_DATE": "2026-06-01",
             "SRC_EFF_END_DATE": "9999-12-31", "BATCH_ID": 20},
            {"CUSTOMER_ID": 2, "NAME": "Globex", "SRC_EFF_START_DATE": "2026-02-01",
             "SRC_EFF_END_DATE": "9999-12-31", "BATCH_ID": 20},
        ]}}}
    c = LocalSqlConnector(broken)
    rule = {"type": "scd2_active_unique",
            "config": {"key_columns": ["CUSTOMER_ID"], "active_sql": scd2_active_predicate(_meta())}}
    out = evaluate(c, _tref(), rule)
    assert out.metric_value == 1 and out.passed is False
    assert out.observed.get("failing_keys"), "offending key should be captured as evidence"


def test_scd2_no_overlap_fails_on_overlapping_intervals():
    overlap = {**SCD2, "tables": {"CUSTOMER_DIM": {
        "columns": COLS,
        "rows": [
            # v1 ends 2026-07-01 but v2 starts 2026-06-01 → one month overlap for customer 1.
            {"CUSTOMER_ID": 1, "NAME": "ACME v1", "SRC_EFF_START_DATE": "2026-01-01",
             "SRC_EFF_END_DATE": "2026-07-01", "BATCH_ID": 10},
            {"CUSTOMER_ID": 1, "NAME": "ACME v2", "SRC_EFF_START_DATE": "2026-06-01",
             "SRC_EFF_END_DATE": "9999-12-31", "BATCH_ID": 20},
        ]}}}
    c = LocalSqlConnector(overlap)
    rule = {"type": "scd2_no_overlap", "config": {
        "key_columns": ["CUSTOMER_ID"],
        "effective_from_column": "SRC_EFF_START_DATE",
        "effective_to_column": "SRC_EFF_END_DATE"}}
    out = evaluate(c, _tref(), rule)
    assert out.metric_value == 1 and out.passed is False


def test_scd2_no_overlap_passes_on_clean_history():
    c = LocalSqlConnector(SCD2)  # cust 1: [01-01,06-01)+[06-01,hi) — touch, don't overlap
    rule = {"type": "scd2_no_overlap", "config": {
        "key_columns": ["CUSTOMER_ID"],
        "effective_from_column": "SRC_EFF_START_DATE",
        "effective_to_column": "SRC_EFF_END_DATE"}}
    out = evaluate(c, _tref(), rule)
    assert out.metric_value == 0 and out.passed is True


# ----------------------------- API: presets, specs, classify -----------------------------
def test_metadata_presets_endpoint_lists_tools(client):
    d = client.get(f"{API}/table-metadata/metadata-presets").json()["data"]
    assert "dbt_snapshot" in d["presets"] and "informatica_dw" in d["presets"]
    assert "effective_to" in d["roles"] and "batch_id" in d["roles"]


def test_pattern_specs_expose_scd2_end_date_fields(client):
    d = client.get(f"{API}/table-metadata/pattern-specs").json()["data"]
    scd2 = next(p for p in d["patterns"] if p["pattern"] == "scd2")
    for f in ("effective_to_column", "business_key", "batch_id_column", "active_mode", "high_date_value"):
        assert f in scd2["fields"]
    assert "effective_to_column" in d["fields"]  # field catalog carries the descriptor


def test_classify_endpoint_is_convention_first(client):
    body = {"columns": [{"name": "C_ID"}, {"name": "VALID_FROM"}, {"name": "EXPIRY"}],
            "convention": {"effective_from": "VALID_FROM", "effective_to": "EXPIRY"}}
    d = client.post(f"{API}/table-metadata/classify", json=body).json()["data"]
    assert d["load_pattern"] == "scd2"
    assert d["effective_from_column"] == "VALID_FROM" and d["effective_to_column"] == "EXPIRY"


# ----------------------------- API: load-aware rule pack + provenance -----------------------------
def _scd2_connection(client) -> str:
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "dw", "auth_method": "none", "coordinates": SCD2},
    ).json()["data"]["id"]


def _scd2_monitor_with_meta(client) -> tuple[str, str]:
    cid = _scd2_connection(client)
    table_ref = "DW.GOLD.CUSTOMER_DIM"
    client.put(f"{API}/table-metadata/{table_ref}", json={
        "load_pattern": "scd2",
        "effective_from_column": "SRC_EFF_START_DATE",
        "effective_to_column": "SRC_EFF_END_DATE",
        "batch_id_column": "BATCH_ID",
        "business_key": ["CUSTOMER_ID"],
        "active_mode": "end_date", "high_date_value": "9999-12-31", "open_end_is_null": True,
    })
    mid = client.post(f"{API}/monitors", json={
        "connection_id": cid, "name": "cust dim",
        "scope": {"database": "DW", "schema": "GOLD", "tables": ["CUSTOMER_DIM"]},
    }).json()["data"]["id"]
    return mid, table_ref


def test_autogenerate_load_aware_scd2_pack(client):
    mid, table_ref = _scd2_monitor_with_meta(client)
    out = client.post(
        f"{API}/monitors/{mid}/tables/{table_ref}/rules:autogenerate-load-aware"
    ).json()["data"]
    by_type = {r["type"]: r for r in out["created"]}
    assert set(by_type) == {"scd2_active_unique", "scd2_no_overlap", "freshness"}
    # Every generated rule is labelled automated.
    assert all(r["origin"] == "system" for r in out["created"])
    assert by_type["scd2_active_unique"]["severity"] == "fail"
    assert by_type["scd2_active_unique"]["state"] == "enabled"
    assert by_type["scd2_no_overlap"]["state"] == "suggested"  # heaviest → off until enabled
    # Active-count baseline metric materialized with a system-origin rule.
    assert out["metrics"] and out["metrics"][0]["rule"]["origin"] == "system"


def test_autogenerate_load_aware_is_idempotent(client):
    mid, table_ref = _scd2_monitor_with_meta(client)
    first = client.post(f"{API}/monitors/{mid}/tables/{table_ref}/rules:autogenerate-load-aware").json()["data"]
    assert len(first["created"]) == 3
    again = client.post(f"{API}/monitors/{mid}/tables/{table_ref}/rules:autogenerate-load-aware").json()["data"]
    assert again["created"] == [] and len(again["skipped"]) == 3  # nothing re-created


def test_editing_a_system_rule_takes_ownership(client):
    mid, table_ref = _scd2_monitor_with_meta(client)
    created = client.post(f"{API}/monitors/{mid}/tables/{table_ref}/rules:autogenerate-load-aware").json()["data"]["created"]
    fresh = next(r for r in created if r["type"] == "freshness")
    other = next(r for r in created if r["type"] == "scd2_active_unique")

    # A content edit flips origin system → user (automation won't overwrite a human decision).
    edited = client.patch(f"{API}/monitors/{mid}/rules/{fresh['id']}",
                          json={"config": {"max_age_hours": 6, "source": "effective_from"}}).json()["data"]
    assert edited["origin"] == "user"

    # A pure enable/disable (state only) keeps the automated label.
    toggled = client.patch(f"{API}/monitors/{mid}/rules/{other['id']}",
                           json={"state": "disabled"}).json()["data"]
    assert toggled["state"] == "disabled" and toggled["origin"] == "system"
