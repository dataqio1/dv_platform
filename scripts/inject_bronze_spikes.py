#!/usr/bin/env python3
"""Inject a clean, 30-day DMS_BRONZE history with DISTINCT, staggered anomalies per table so
every observability pillar shows an obvious spike in the UI. Credential-free: it only POSTs
the profiler's snapshot write-path (no Snowflake access needed).

Each table gets its *own* anomaly on its *own* day/metric, so the monitor tells a varied story:

  BRZ_ORDERS        volume SPIKE (~3.2x) 6d ago  + quality drop / null burst 13d ago
  BRZ_EVENTS        volume DIP (~7%) 10d ago (collector outage)
  BRZ_PAGEVIEWS     volume SPIKE (~2.7x) 3d ago (campaign)
  BRZ_CUSTOMER_SCD2 volume SPIKE (~4x) 16d ago (backfill batch)
  BRZ_CUSTOMERS     null-rate + distribution SHIFT 8d ago
  BRZ_PRODUCTS      quality DROP 5d ago
  BRZ_INVENTORY     FRESHNESS — loads stop 3d ago (moderately stale)
  BRZ_LEGACY_FEED   FRESHNESS — loads stop 8d ago (severely stale)

Run the clear step first (see inject_bronze_spikes.sh) so points aren't duplicated.
Env: DVP_API (default http://127.0.0.1:8000), BRONZE_MONITOR_ID.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import urllib.request

API = os.environ.get("DVP_API", "http://127.0.0.1:8000") + "/api/v1"
MID = os.environ.get("BRONZE_MONITOR_ID", "200b1189-f701-4064-bd22-70168ca9b916")
DB, SCHEMA = "DWH_TEST", "DMS_BRONZE"
DAYS = 30
random.seed(7)
TODAY = dt.date.today()
WINDOW = [TODAY - dt.timedelta(days=DAYS - 1 - i) for i in range(DAYS)]


def post(table: str, body: dict) -> None:
    ref = f"{DB}.{SCHEMA}.{table}"
    req = urllib.request.Request(
        f"{API}/monitors/{MID}/tables/{ref}/snapshot",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=30).read()


def iso(d: dt.date, h: int = 12, m: int = 0) -> str:
    return f"{d.isoformat()}T{h:02d}:{m:02d}:00Z"


def organic(base: float, i: int, growth: float = 0.01, noise: float = 0.04) -> float:
    return base * (1 + growth * i) * random.uniform(1 - noise, 1 + noise)


def days_ago(i: int) -> int:
    return DAYS - 1 - i


# ---- per-table profiles: (table, generator(i, day) -> observation dict | None) ----
def brz_orders(i, d):  # noqa: ANN001
    n = organic(5000, i)
    q, nulls, dist = 98.0, 0.01, 0.0008
    if days_ago(i) == 6:
        n *= 3.2  # big volume spike
    if days_ago(i) == 13:
        q, nulls = 76.0, 0.24  # quality drop + null burst on STATUS
    return {
        "row_count": int(n), "last_load_ts": iso(d, 23, 55), "quality_score": round(q, 1),
        "columns": [
            {"name": "STATUS", "data_type": "STRING", "null_rate": round(nulls, 3), "distinct_ratio": dist},
            {"name": "AMOUNT", "data_type": "NUMBER", "null_rate": 0.0, "distinct_ratio": 0.42},
        ],
        "ts": iso(d),
    }


def brz_events(i, d):  # noqa: ANN001
    n = organic(22000, i)
    if days_ago(i) == 10:
        n *= 0.07  # near-outage dip
    return {"row_count": int(n), "last_load_ts": iso(d, 23, 50), "quality_score": 97.0, "ts": iso(d)}


def brz_pageviews(i, d):  # noqa: ANN001
    n = organic(48000, i)
    if days_ago(i) == 3:
        n *= 2.7  # campaign spike
    return {"row_count": int(n), "last_load_ts": iso(d, 23, 40), "quality_score": 96.5, "ts": iso(d)}


def brz_customers(i, d):  # noqa: ANN001
    n = int(organic(12000, i, growth=0.004, noise=0.01))
    nr, dist, q = 0.02, 0.9, 97.0
    if days_ago(i) == 8:
        nr, dist, q = 0.31, 0.55, 82.0  # null + distribution shift
    return {
        "row_count": n, "last_load_ts": iso(d, 2, 0), "quality_score": q,
        "columns": [
            {"name": "EMAIL", "data_type": "STRING", "null_rate": round(nr, 3), "distinct_ratio": 0.98},
            {"name": "COUNTRY", "data_type": "STRING", "null_rate": 0.0, "distinct_ratio": round(dist, 3)},
        ],
        "ts": iso(d),
    }


def brz_scd2(i, d):  # noqa: ANN001
    n = organic(3000, i)
    if days_ago(i) == 16:
        n *= 4.0  # backfill batch spike
    return {"row_count": int(n), "last_load_ts": iso(d, 1, 0), "quality_score": 98.0, "ts": iso(d)}


def brz_products(i, d):  # noqa: ANN001
    n = int(2000 + i * 2 + random.randint(-5, 5))
    q = 88.0 if days_ago(i) == 5 else 99.0  # mild quality dip
    return {"row_count": n, "last_load_ts": iso(d, 3, 0), "quality_score": q, "ts": iso(d)}


def brz_inventory(i, d):  # noqa: ANN001
    if days_ago(i) < 3:
        return None  # loads stopped 3 days ago → moderately stale
    return {"row_count": int(organic(8000, i, growth=0.002, noise=0.02)),
            "last_load_ts": iso(d, 4, 0), "quality_score": 97.0, "ts": iso(d)}


def brz_legacy(i, d):  # noqa: ANN001
    if days_ago(i) < 8:
        return None  # loads stopped 8 days ago → severely stale
    return {"row_count": int(organic(350, i, growth=0.005, noise=0.03)),
            "last_load_ts": iso(d, 6, 0), "quality_score": 95.0, "ts": iso(d)}


PROFILES = [
    ("BRZ_ORDERS", brz_orders),
    ("BRZ_EVENTS", brz_events),
    ("BRZ_PAGEVIEWS", brz_pageviews),
    ("BRZ_CUSTOMERS", brz_customers),
    ("BRZ_CUSTOMER_SCD2", brz_scd2),
    ("BRZ_PRODUCTS", brz_products),
    ("BRZ_INVENTORY", brz_inventory),
    ("BRZ_LEGACY_FEED", brz_legacy),
]


def main() -> None:
    print(f"Injecting {DAYS}-day bronze history → monitor {MID[:8]} ({WINDOW[0]} .. {WINDOW[-1]})\n")
    for table, gen in PROFILES:
        posted = 0
        for i, d in enumerate(WINDOW):  # oldest → newest
            obs = gen(i, d)
            if obs is None:
                continue
            post(table, obs)
            posted += 1
        gap = DAYS - posted
        note = f" (stale: last load {gap}d ago)" if gap >= 2 else ""
        print(f"  {table:<20} {posted} days{note}")
    print("\nDone. Open the DMS_BRONZE Medallion Monitor to see the spikes.")


if __name__ == "__main__":
    main()
