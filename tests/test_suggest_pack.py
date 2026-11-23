"""Monitor-level, sample-based suggested rule-pack generation over a REAL local SQL source.

Mirrors the customer's mixed-layer reality: one monitor spanning a SOURCE table that carries ETL
metadata columns and a CURATE table that has NONE. Verifies the pack is generated from a sample
(not a full scan), lands as state='suggested'/origin='system' for accept-or-remove, covers the
metadata-less table with column rules, is idempotent, and supports table selection + preview.
"""

from __future__ import annotations

from datetime import UTC, datetime

from dvp.connectors.base import TableRef
from dvp.connectors.local_sql import LocalSqlConnector
from dvp.observability.evaluator import evaluate

API = "/api/v1"

# One connection, two layers. ORDERS = source (has INGESTED_AT load column). CUST = curate
# (no metadata columns at all — just business data).
COORDS = {
    "database": "DW",
    "schema": "SRC",
    "tables": {
        "ORDERS": {
            "columns": [
                {"name": "ORDER_ID", "data_type": "NUMBER", "nullable": False},
                {"name": "AMOUNT", "data_type": "NUMBER", "nullable": False},
                {"name": "INGESTED_AT", "data_type": "TIMESTAMP", "nullable": False},
            ],
            "rows": [
                {"ORDER_ID": 1, "AMOUNT": 10, "INGESTED_AT": "2026-11-01T00:00:00"},
                {"ORDER_ID": 2, "AMOUNT": 20, "INGESTED_AT": "2026-11-02T00:00:00"},
                {"ORDER_ID": 3, "AMOUNT": 30, "INGESTED_AT": "2026-11-03T00:00:00"},
            ],
        },
        "CUST": {  # curate layer — NO metadata/audit columns
            "columns": [
                {"name": "CUSTOMER_ID", "data_type": "NUMBER", "nullable": False},
                {"name": "NAME", "data_type": "VARCHAR", "nullable": False},
                {"name": "EMAIL", "data_type": "VARCHAR", "nullable": True},
            ],
            "rows": [
                {"CUSTOMER_ID": 1, "NAME": "ACME", "EMAIL": "a@x.com"},
                {"CUSTOMER_ID": 2, "NAME": "Globex", "EMAIL": None},
            ],
        },
    },
}


def _monitor(client, *, watermark: str | None = None) -> str:
    cid = client.post(f"{API}/connections", json={
        "source_type": "local", "name": "dw", "auth_method": "none", "coordinates": COORDS,
    }).json()["data"]["id"]
    body = {"connection_id": cid, "name": "src+curate",
            "scope": {"database": "DW", "schema": "SRC", "tables": ["ORDERS", "CUST"]}}
    if watermark:
        body["watermark_column"] = watermark
    return client.post(f"{API}/monitors", json=body).json()["data"]["id"]


def test_suggest_pack_covers_metadata_less_curate_table(client):
    # Monitor spans a source table (load column) + a curate table (none); the monitor-level
    # watermark exists only in ORDERS.
    mid = _monitor(client, watermark="INGESTED_AT")
    d = client.post(f"{API}/monitors/{mid}/rules:suggest-pack",
                    json={"sample_size": 1000, "level": 3}).json()["data"]

    assert d["persisted"] is True
    by_table = {t["table_ref"]: t for t in d["tables"]}
    # Both tables were sampled (no full scan: sampled ≤ sample_size), including the curate one.
    assert "DW.SRC.CUST" in by_table and "error" not in by_table["DW.SRC.CUST"]
    assert by_table["DW.SRC.CUST"]["sampled"] == 2

    created = d["created"]
    assert created, "rules should have been created"
    # Everything comes in as suggested + automated, ready for accept/remove.
    assert all(r["state"] == "suggested" and r["origin"] == "system" for r in created)

    # The curate table (no metadata cols) still gets real column rules from profiling.
    cust = [r for r in created if r["table_ref"] == "DW.SRC.CUST"]
    types = {r["type"] for r in cust}
    assert "not_null" in types   # CUSTOMER_ID / NAME are non-null in the sample
    assert "row_count" in types


def test_suggest_pack_is_idempotent(client):
    mid = _monitor(client)
    first = client.post(f"{API}/monitors/{mid}/rules:suggest-pack", json={}).json()["data"]
    assert len(first["created"]) > 0
    again = client.post(f"{API}/monitors/{mid}/rules:suggest-pack", json={}).json()["data"]
    assert again["created"] == [] and len(again["skipped"]) == len(first["created"])


def test_suggest_pack_respects_table_selection(client):
    mid = _monitor(client)
    d = client.post(f"{API}/monitors/{mid}/rules:suggest-pack",
                    json={"tables": ["CUST"]}).json()["data"]
    assert d["tables_selected"] == 1
    assert {r["table_ref"] for r in d["created"]} == {"DW.SRC.CUST"}  # ORDERS skipped by selection


def test_suggest_pack_preview_persists_nothing(client):
    mid = _monitor(client)
    d = client.post(f"{API}/monitors/{mid}/rules:suggest-pack",
                    json={"persist": False}).json()["data"]
    assert d["persisted"] is False
    # Preview carries the specs + a dry-run verdict per rule, but creates no rules.
    assert any(r.get("dry_run") for t in d["tables"] for r in t["rules"])
    assert client.get(f"{API}/monitors/{mid}/rules").json()["data"] == []


# ----------------------------- warehouse LAST_ALTERED freshness (no load column) -----------------------------
def _wh_connector(last_altered: str) -> LocalSqlConnector:
    return LocalSqlConnector({
        "database": "DW", "schema": "PUB",
        "tables": {"REPORT": {
            "columns": [{"name": "METRIC", "data_type": "VARCHAR"}],
            "rows": [{"METRIC": "x"}],
            "last_altered": last_altered,  # stands in for INFORMATION_SCHEMA.LAST_ALTERED
        }},
    })


def _report_ref() -> TableRef:
    return TableRef("DW", "PUB", "REPORT")


def test_warehouse_freshness_passes_when_recently_altered():
    now = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
    c = _wh_connector("2026-11-06T06:00:00")  # 6h old
    rule = {"type": "freshness", "columns": [], "config": {"source": "warehouse", "max_age_hours": 24}}
    out = evaluate(c, _report_ref(), rule, now=now)
    assert out.passed is True and out.observed["source"] == "warehouse"


def test_warehouse_freshness_fails_when_stale():
    now = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
    c = _wh_connector("2026-11-01T00:00:00")  # >5 days old
    rule = {"type": "freshness", "columns": [], "config": {"source": "warehouse", "max_age_hours": 24}}
    out = evaluate(c, _report_ref(), rule, now=now)
    assert out.passed is False


def test_warehouse_freshness_unavailable_is_surfaced():
    now = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
    c = _wh_connector(None)  # no last-altered known
    rule = {"type": "freshness", "columns": [], "config": {"source": "warehouse", "max_age_hours": 24}}
    out = evaluate(c, _report_ref(), rule, now=now)
    assert out.passed is False and "unavailable" in out.observed["reason"]
