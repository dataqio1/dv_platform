#!/usr/bin/env python3
"""Backfill N days of daily observability history for the medallion monitors.

Each table is normally profiled once (a single scan -> a single chart point). To make the
trend charts show a real N-day line, this records one daily snapshot per table per day, with
the *actual* per-day row count from Snowflake (so volume dips/spikes and the stale feed show
up), oldest -> newest so anomaly detection builds correctly.

Env (same as the seeder): SF_ACCOUNT, SF_USER, SF_KEY, SF_KEY_PWD, SF_WH.
API base via DVP_API (default http://127.0.0.1:8000). DAYS via HIST_DAYS (default 30).
Monitor ids are read from the running API by monitor name.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import urllib.request

import snowflake.connector as sc

API = os.environ.get("DVP_API", "http://127.0.0.1:8000") + "/api/v1"
DB = "DWH_TEST"
DAYS = int(os.environ.get("HIST_DAYS", "30"))
TODAY = dt.date.today()
WINDOW = [TODAY - dt.timedelta(days=DAYS - 1 - i) for i in range(DAYS)]

# (schema, table, daily_time_column | None).  None -> snapshot table: flat total each day.
TABLES = [
    ("DMS_BRONZE", "BRZ_ORDERS", "LOAD_TS"),
    ("DMS_BRONZE", "BRZ_EVENTS", "LOAD_TS"),
    ("DMS_BRONZE", "BRZ_CUSTOMERS", None),
    ("DMS_BRONZE", "BRZ_PRODUCTS", None),
    ("DMS_BRONZE", "BRZ_PAGEVIEWS", "LOAD_TS"),
    ("DMS_BRONZE", "BRZ_INVENTORY", None),
    ("DMS_BRONZE", "BRZ_LEGACY_FEED", "LOAD_TS"),
    ("DMS_BRONZE", "BRZ_CUSTOMER_SCD2", "LOAD_TS"),
    ("DMS_SILVER", "SLV_FCT_ORDERS", "ORDER_DATE"),
    ("DMS_SILVER", "SLV_FCT_EVENTS", "EVENT_DATE"),
    ("DMS_SILVER", "SLV_DIM_CUSTOMER", "LOADED_AT"),
    ("DMS_SILVER", "SLV_DIM_PRODUCT", None),
    ("DMS_SILVER", "SLV_DIM_DATE", None),
    ("DMS_GOLD", "GLD_KPI_DAILY", "METRIC_DATE"),
    ("DMS_GOLD", "GLD_DAILY_SALES_BY_REGION", "METRIC_DATE"),
    ("DMS_GOLD", "GLD_CUSTOMER_360", None),
    ("DMS_GOLD", "GLD_PRODUCT_PERFORMANCE_DAILY", "METRIC_DATE"),
    ("DMS_GOLD", "GLD_FUNNEL_DAILY", "METRIC_DATE"),
]
MONITOR_BY_SCHEMA = {
    "DMS_BRONZE": "DMS_BRONZE Medallion Monitor",
    "DMS_SILVER": "DMS_SILVER Medallion Monitor",
    "DMS_GOLD": "DMS_GOLD Medallion Monitor",
}


def api_get(path: str):
    with urllib.request.urlopen(API + path, timeout=30) as r:
        return json.load(r)["data"]


def api_post(path: str, body: dict):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def connect():
    pwd = os.environ.get("SF_KEY_PWD") or ""
    kw = dict(account=os.environ["SF_ACCOUNT"], user=os.environ["SF_USER"],
              private_key_file=os.environ["SF_KEY"],
              warehouse=os.environ.get("SF_WH", "SNOWFLAKE_LEARNING_WH"), login_timeout=30)
    if pwd:
        kw["private_key_file_pwd"] = pwd.encode()
    return sc.connect(**kw)


def daily_counts(cur, schema: str, table: str, col: str) -> dict[str, int] | None:
    """Per-day counts by `col`; returns None if the column doesn't exist (caller falls back)."""
    try:
        cur.execute(
            f'SELECT TO_VARCHAR(TO_DATE("{col}")) d, COUNT(*) c FROM {DB}.{schema}."{table}" '
            f'WHERE "{col}" >= DATEADD(day, -{DAYS}, CURRENT_DATE) GROUP BY 1'
        )
        return {r[0]: int(r[1]) for r in cur.fetchall() if r[0]}
    except sc.errors.ProgrammingError as e:
        if "invalid identifier" in str(e):
            return None
        raise


def total_rows(cur, schema: str, table: str) -> int:
    cur.execute(f'SELECT COUNT(*) FROM {DB}.{schema}."{table}"')
    return int(cur.fetchone()[0])


def main() -> None:
    monitors = {m["name"]: m["id"] for m in api_get("/monitors")}
    mon_id = {s: monitors[n] for s, n in MONITOR_BY_SCHEMA.items() if n in monitors}
    missing = [n for n in MONITOR_BY_SCHEMA.values() if n not in monitors]
    if missing:
        print("WARNING: monitors not found:", missing)

    only = {t.strip().upper() for t in os.environ.get("HIST_TABLES", "").split(",") if t.strip()}
    con = connect()
    cur = con.cursor()
    print(f"Backfilling {DAYS} days ({WINDOW[0]} .. {WINDOW[-1]}) into the medallion monitors\n")
    for schema, table, col in TABLES:
        mid = mon_id.get(schema)
        if not mid or (only and table.upper() not in only):
            continue
        ref = f"{DB}.{schema}.{table}"
        counts = daily_counts(cur, schema, table, col) if col else None
        if counts is not None:
            series = [(d, counts.get(d.isoformat(), 0)) for d in WINDOW]
            # only record days that actually loaded (natural gaps = the stale story)
            series = [(d, c) for d, c in series if c > 0]
        else:
            total = total_rows(cur, schema, table)
            series = [(d, total) for d in WINDOW]   # snapshot / no daily column: flat total each day

        posted = 0
        for d, c in series:                          # oldest -> newest
            body = {"row_count": c, "ts": f"{d.isoformat()}T12:00:00Z",
                    "last_load_ts": f"{d.isoformat()}T23:59:00Z"}
            try:
                api_post(f"/monitors/{mid}/tables/{ref}/snapshot", body)
                posted += 1
            except Exception as e:  # noqa: BLE001
                print(f"  {table}: post failed on {d}: {str(e)[:60]}")
                break
        print(f"  {schema}.{table:30} {posted:>3} daily points")
    con.close()
    print("\nDone. Reload a table page — the trend/volume charts now span the window.")


if __name__ == "__main__":
    main()
