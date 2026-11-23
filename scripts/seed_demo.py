#!/usr/bin/env python3
"""Seed the running DV Platform with rich demo data for good visuals.

Creates a medallion-ish setup (bronze/silver/gold tables across a few sources),
monitors + custom/built-in rules, then backfills ~14 days of pillar + rule history
(with trends and a couple of anomalies so charts look alive), and a handful of Data
Validation compares (count / schema / data) with real differences.

Run against a live server:  PYTHONPATH=src .venv/bin/dvp run   then   python scripts/seed_demo.py
"""

from __future__ import annotations

import json
import random
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "http://127.0.0.1:8000/api/v1"
random.seed(42)
NOW = datetime.now(timezone.utc).replace(microsecond=0)
DAYS = [NOW - timedelta(days=d) for d in range(13, -1, -1)]  # oldest → newest


def _req(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"content-type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req) as r:  # noqa: S310
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        print(f"  ! {method} {path} -> {e.code}: {e.read().decode()[:200]}")
        return {}


def post(path, body):
    return _req("POST", path, body).get("data")


def iso(dt: datetime) -> str:
    return dt.isoformat()


# ----------------------------- medallion monitored tables (observability) -----------------------------
# Inline connections (schema only) — history is seeded via snapshots/records below.
LAYERS = [
    {
        "source": "bronze_postgres", "layer": "bronze",
        "tables": {
            "RAW_ORDERS": {"base_rows": 42000, "quality": 97, "null_col": "EMAIL",
                           "cols": ["ORDER_ID", "CUSTOMER_ID", "AMOUNT", "EMAIL", "INGESTED_AT"]},
            "RAW_CUSTOMERS": {"base_rows": 8800, "quality": 92, "null_col": "PHONE",
                              "cols": ["CUSTOMER_ID", "NAME", "PHONE", "COUNTRY", "INGESTED_AT"]},
        },
    },
    {
        "source": "warehouse", "layer": "silver",
        "tables": {
            "STG_ORDERS": {"base_rows": 41800, "quality": 99, "null_col": "DISCOUNT",
                           "cols": ["ORDER_ID", "CUSTOMER_ID", "NET_AMOUNT", "DISCOUNT", "LOADED_AT"]},
        },
    },
    {
        "source": "warehouse_gold", "layer": "gold",
        "tables": {
            "FCT_REVENUE": {"base_rows": 365, "quality": 100, "null_col": None,
                            "cols": ["DATE_KEY", "REGION", "REVENUE", "ORDERS", "LOADED_AT"]},
        },
    },
]


def _coords(tables: dict) -> dict:
    out = {"database": "ANALYTICS", "schema": "PUBLIC", "tables": {}}
    for t, cfg in tables.items():
        wm = "INGESTED_AT" if "INGESTED_AT" in cfg["cols"] else "LOADED_AT"
        out["tables"][t] = {
            "columns": [
                {"name": c, "data_type": "TIMESTAMP_NTZ" if c.endswith("_AT") else
                 ("NUMBER" if c in ("ORDER_ID", "CUSTOMER_ID", "AMOUNT", "NET_AMOUNT",
                                    "DISCOUNT", "REVENUE", "ORDERS", "DATE_KEY") else "VARCHAR"),
                 "nullable": c == cfg["null_col"]}
                for c in cfg["cols"]
            ],
            "watermarks": {wm: iso(NOW)},
        }
    return out


def seed_observability() -> None:
    print("▶ observability: sources, monitors, rules, 14-day history")
    for layer in LAYERS:
        conn = post("/connections", {
            "source_type": "inline", "name": layer["source"], "auth_method": "none",
            "coordinates": _coords(layer["tables"]),
        })
        if not conn:
            continue
        wm = None
        for tname, cfg in layer["tables"].items():
            wm = "INGESTED_AT" if "INGESTED_AT" in cfg["cols"] else "LOADED_AT"
            # bronze raw = append, silver = incremental, gold fact = truncate & load
            load_type = {"bronze": "append", "silver": "incremental", "gold": "full_reload"}.get(
                layer["layer"], "incremental"
            )
            mon = post("/monitors", {
                "connection_id": conn["id"], "name": f"{layer['layer']}:{tname}",
                "scope": {"database": "ANALYTICS", "schema": "PUBLIC", "tables": [tname]},
                "watermark_column": wm, "watermark_type": "timestamp",
                "load_type": load_type,
                "tags": [layer["layer"]],
            })
            if not mon:
                continue
            fqn = f"ANALYTICS.PUBLIC.{tname}"

            # ---- 14 days of pillar history ----
            rows = cfg["base_rows"]
            for i, day in enumerate(DAYS):
                rows = int(rows * random.uniform(1.0, 1.03))  # organic growth
                # inject a volume dip on day 9 for bronze orders (anomaly to see)
                if tname == "RAW_ORDERS" and i == 9:
                    rows = int(rows * 0.55)
                null_rate = round(min(0.25, 0.01 + i * 0.004 + random.uniform(0, 0.01)), 4)
                quality = max(60, cfg["quality"] - (null_rate * 100) * 0.6 - (6 if i == 9 and tname == "RAW_ORDERS" else 0))
                cols_payload = []
                for c in cfg["cols"]:
                    if c.endswith("_AT"):
                        continue
                    cols_payload.append({
                        "name": c, "data_type": "NUMBER" if c.isupper() else "STRING",
                        "null_rate": null_rate if c == cfg["null_col"] else round(random.uniform(0, 0.005), 4),
                    })
                post(f"/monitors/{mon['id']}/tables/{fqn}/snapshot", {
                    "row_count": rows,
                    "last_load_ts": iso(day.replace(hour=2, minute=random.randint(0, 59))),
                    "columns": cols_payload,
                    "quality_score": round(quality, 1),
                    "ts": iso(day),
                })

            # ---- rules + 14 days of rule results ----
            rules = [
                {"name": f"row_count_{tname.lower()}", "type": "row_count",
                 "table_ref": fqn, "config": {"min_rows": int(cfg["base_rows"] * 0.6)}},
                {"name": "no_negative_amount", "type": "custom_sql", "table_ref": fqn,
                 "severity": "critical",
                 "config": {"expression": 'SELECT COUNT(*) AS v FROM {{table}} WHERE "AMOUNT" < 0',
                            "threshold": 0, "dimension": "quality"}},
            ]
            if cfg["null_col"]:
                rules.append({"name": f"{cfg['null_col'].lower()}_not_null", "type": "not_null",
                              "columns": [cfg["null_col"]], "table_ref": fqn, "severity": "warning",
                              "config": {"threshold": 0}})
            for rspec in rules:
                rule = post(f"/monitors/{mon['id']}/rules", rspec)
                if not rule:
                    continue
                for i, day in enumerate(DAYS):
                    if rspec["type"] == "row_count":
                        val = int(cfg["base_rows"] * (0.55 if (tname == "RAW_ORDERS" and i == 9) else 1.0))
                    elif rspec["type"] == "not_null":
                        val = 0 if i < 10 else random.randint(1, 40)  # starts failing late
                    else:  # no_negative_amount
                        val = 0 if i not in (6, 11) else random.randint(1, 5)
                    post(f"/monitors/{mon['id']}/rules/{rule['id']}:record",
                         {"table_ref": fqn, "metric_value": val, "ts": iso(day)})
    print("  ✓ observability seeded")


# ----------------------------- data validation (compares) -----------------------------
def _orders_rows(n: int, mutate: bool = False):
    rows = []
    for i in range(1, n + 1):
        amt = 10 * i
        status = "OPEN" if i % 3 else "CLOSED"
        if mutate and i % 7 == 0:
            amt += 5  # value drift
        if mutate and i % 11 == 0:
            status = "PENDING"  # value drift
        rows.append({"ORDER_ID": i, "CUSTOMER_ID": 1000 + (i % 50),
                     "AMOUNT": amt, "STATUS": status})
    if mutate:
        rows = rows[:-2]  # two src-only rows (keys only in source)
        rows.append({"ORDER_ID": n + 1, "CUSTOMER_ID": 2000, "AMOUNT": 999, "STATUS": "NEW"})  # dst-only
        rows.append({"ORDER_ID": 5, "CUSTOMER_ID": 1005, "AMOUNT": 51, "STATUS": "OPEN"})  # dup key → ambiguous
    return rows


ORD_COLS = [
    {"name": "ORDER_ID", "data_type": "NUMBER", "nullable": False},
    {"name": "CUSTOMER_ID", "data_type": "NUMBER", "nullable": False},
    {"name": "AMOUNT", "data_type": "NUMBER", "nullable": False},
    {"name": "STATUS", "data_type": "VARCHAR", "nullable": True},
]


def seed_table_patterns() -> None:
    """Simulate a customer's custom ingestion software injecting per-table load patterns."""
    print("▶ injecting load patterns (external ingestion)")
    _req("PUT", "/table-metadata", {
        "source": "acme_ingestion",
        "items": [
            {"table_ref": "ANALYTICS.PUBLIC.RAW_ORDERS", "load_pattern": "append",
             "watermark_column": "INGESTED_AT", "watermark_type": "timestamp"},
            {"table_ref": "ANALYTICS.PUBLIC.RAW_CUSTOMERS", "load_pattern": "cdc",
             "changed_at_column": "INGESTED_AT"},
            {"table_ref": "ANALYTICS.PUBLIC.STG_ORDERS", "load_pattern": "scd2",
             "effective_from_column": "LOADED_AT", "current_flag_column": "IS_CURRENT"},
            {"table_ref": "ANALYTICS.PUBLIC.FCT_REVENUE", "load_pattern": "partitioned",
             "partition_column": "DATE_KEY"},
        ],
    })
    print("  ✓ load patterns injected")


def seed_projects_and_tags() -> str | None:
    """A project to group the validation test cases + a few managed tags."""
    print("▶ project + tags")
    for t in [
        {"name": "critical", "color": "#ef4444"},
        {"name": "pii", "color": "#f59e0b"},
        {"name": "migration", "color": "#14b8a6"},
    ]:
        post("/tags", t)
    proj = post("/projects", {"name": "Finance Warehouse Migration", "tags": ["migration"]})
    return proj["id"] if proj else None


# A table with deliberately varied cardinality + nulls + empties, so the profile charts
# (counts / nulls+empty / unique) are interesting. Backed by real SQLite (source_type=local)
# so the profile report's pushdown aggregates actually run.
def _events_rows(n: int):
    regions = ["US", "EU", "APAC", "LATAM"]
    statuses = ["OPEN", "CLOSED", "PENDING"]
    rows = []
    for i in range(1, n + 1):
        email = f"user{i}@acme.io"
        if i % 11 == 0:
            email = None  # null
        elif i % 17 == 0:
            email = ""  # empty
        rows.append(
            {
                "EVENT_ID": i,  # unique (high cardinality)
                "USER_ID": 1000 + (i % 120),  # medium cardinality
                "EMAIL": email,  # high cardinality + nulls + empties
                "STATUS": statuses[i % 3],  # low cardinality
                "REGION": regions[i % 4] if i % 23 else None,  # low cardinality + nulls
                "AMOUNT": round((i % 50) * 3.5, 2),
            }
        )
    return rows


EVENT_COLS = [
    {"name": "EVENT_ID", "data_type": "NUMBER"},
    {"name": "USER_ID", "data_type": "NUMBER"},
    {"name": "EMAIL", "data_type": "VARCHAR"},
    {"name": "STATUS", "data_type": "VARCHAR"},
    {"name": "REGION", "data_type": "VARCHAR"},
    {"name": "AMOUNT", "data_type": "NUMBER"},
]


def seed_reports(project_id: str | None) -> None:
    print("▶ reports: profile + validation + executive")
    events = post("/connections", {
        "source_type": "local", "name": "events_lake", "auth_method": "none",
        "coordinates": {"database": "LAKE", "schema": "PUBLIC",
                        "tables": {"RAW_EVENTS": {"columns": EVENT_COLS, "rows": _events_rows(600)}}},
    })
    if events:
        prof = post("/reports", {
            "name": "RAW_EVENTS data profile", "report_type": "profile",
            "spec": {"connection_id": events["id"], "schema": "PUBLIC", "tables": ["RAW_EVENTS"]},
        })
        if prof:
            post(f"/reports/{prof['id']}:generate", None)

    val = post("/reports", {
        "name": "Migration validation", "report_type": "data_validation",
        "project_id": project_id,
    })
    if val:  # two versions so the trend chart + version history render
        post(f"/reports/{val['id']}:generate", None)
        post(f"/reports/{val['id']}:generate", None)

    execrep = post("/reports", {"name": "Executive data health", "report_type": "executive"})
    if execrep:
        post(f"/reports/{execrep['id']}:generate", None)
    print("  ✓ reports seeded")


def seed_validation(project_id: str | None = None) -> None:
    print("▶ data validation: prod/staging connections + compares")
    prod = post("/connections", {
        "source_type": "local", "name": "prod_warehouse", "auth_method": "none",
        "coordinates": {"database": "PROD", "schema": "PUBLIC",
                        "tables": {"ORDERS": {"columns": ORD_COLS, "rows": _orders_rows(60)}}},
    })
    staging = post("/connections", {
        "source_type": "local", "name": "staging_warehouse", "auth_method": "none",
        "coordinates": {"database": "STAGING", "schema": "PUBLIC",
                        "tables": {"ORDERS": {"columns": ORD_COLS, "rows": _orders_rows(60, mutate=True)}}},
    })
    # schema-drift target: extra column + retyped
    drift_cols = ORD_COLS + [{"name": "REGION", "data_type": "VARCHAR", "nullable": True}]
    drift = post("/connections", {
        "source_type": "local", "name": "staging_v2", "auth_method": "none",
        "coordinates": {"database": "STAGING", "schema": "PUBLIC",
                        "tables": {"ORDERS": {"columns": drift_cols, "rows": _orders_rows(58)}}},
    })
    if not (prod and staging and drift):
        return

    def compare(kind, name, s, t, options=None, tags=None):
        job = post("/compares", {
            "kind": kind, "name": name,
            "source": {"connection_id": s, "table": "ORDERS"},
            "target": {"connection_id": t, "table": "ORDERS"},
            "options": options or {},
            "project_id": project_id, "source_system": "snowflake", "layer": "silver",
            "tags": tags or [],
        })
        if job:
            post(f"/compares/{job['id']}:run", None)

    compare("data", "ORDERS: prod vs staging (cell-level)", prod["id"], staging["id"], tags=["critical"])
    compare("schema", "ORDERS: schema drift check", prod["id"], drift["id"])
    compare("count", "ORDERS: row-count reconciliation", prod["id"], staging["id"], {"tolerance_pct": 1})
    compare("data", "ORDERS: prod vs prod (clean)", prod["id"], prod["id"])
    print("  ✓ validation seeded")


def main() -> None:
    health = _req("GET", "/health")
    if not health:
        print("Server not reachable at", BASE, "— start it with `dvp run` first.")
        return
    seed_observability()
    seed_table_patterns()
    project_id = seed_projects_and_tags()
    seed_validation(project_id)
    seed_reports(project_id)
    print("\n✅ Demo data seeded. Open http://127.0.0.1:5173")


if __name__ == "__main__":
    main()
