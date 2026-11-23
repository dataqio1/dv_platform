#!/usr/bin/env python3
"""Create data-observability monitors over the S3 file scenarios.

Point this at a running DV Platform API; it creates one S3 connection + monitor per file
scenario seeded by seed_s3_file_scenarios.py, each with a file-arrival freshness alert policy,
then profiles every table once so the pillars populate.

    # start the API first:  PYTHONPATH=src .venv/bin/dvp run
    python scripts/seed_file_monitors.py
    python scripts/seed_file_monitors.py --base http://app.vexdata.io/api/v1 --no-profile

The server process must have boto3 + AWS credentials (env / instance role) to read the bucket.
A profiling failure for one table is reported and does not stop the rest. Re-running creates
additional monitors (connections/monitors are not de-duplicated by name).
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("DVP_API_BASE", "http://127.0.0.1:8000/api/v1")
BUCKET = os.environ.get("DVP_S3_BUCKET", "dataq-testing-data")
PREFIX = os.environ.get("DVP_S3_PREFIX", "observability")


def _uri(*parts: str) -> str:
    return f"s3://{BUCKET}/{PREFIX}/" + "/".join(parts)


# A per-pillar alert policy with file-arrival freshness (the point of the file feature).
def _policy(cadence_h: float, warn_h: float, severe_h: float) -> dict:
    return {
        "freshness": {
            "source": "file_arrival",
            "cadence_hours": cadence_h,
            "warn_after_hours": warn_h,
            "severe_after_hours": severe_h,
        },
        "quality": {"warn_below": 80, "severe_below": 60},
        "volume": {"warn_pct": None, "severe_pct": None},
    }


# scenario = (monitor name, connection coordinates, scope, [table_refs to profile])
SCENARIOS = [
    (
        "Orders file (CSV)",
        {"database": "S3", "schema": "SINGLE", "uri": _uri("single", "orders.csv"),
         "format": "csv", "table": "ORDERS"},
        {"database": "S3", "schema": "SINGLE", "tables": ["ORDERS"]},
        ["S3.SINGLE.ORDERS"],
        _policy(24, 26, 48),
    ),
    (
        "Customers file (TSV)",
        {"database": "S3", "schema": "SINGLE", "uri": _uri("single", "customers.tsv"),
         "format": "tsv", "table": "CUSTOMERS"},
        {"database": "S3", "schema": "SINGLE", "tables": ["CUSTOMERS"]},
        ["S3.SINGLE.CUSTOMERS"],
        _policy(24, 26, 48),
    ),
    (
        "Events file (NDJSON)",
        {"database": "S3", "schema": "SINGLE", "uri": _uri("single", "events.ndjson"),
         "format": "ndjson", "table": "EVENTS"},
        {"database": "S3", "schema": "SINGLE", "tables": ["EVENTS"]},
        ["S3.SINGLE.EVENTS"],
        _policy(1, 2, 6),
    ),
    (
        "Sales file (Parquet)",
        {"database": "S3", "schema": "SINGLE", "uri": _uri("parquet", "sales.parquet"),
         "format": "parquet", "table": "SALES"},
        {"database": "S3", "schema": "SINGLE", "tables": ["SALES"]},
        ["S3.SINGLE.SALES"],
        _policy(24, 26, 48),
    ),
    (
        "Data lake (scan prefix)",
        {"uri": _uri("lake") + "/", "scan": True},
        {"tables": ["REGIONS", "SHIPMENTS", "PAYMENTS"]},
        ["REGIONS", "SHIPMENTS", "PAYMENTS"],
        _policy(24, 26, 48),
    ),
    (
        "Scheduled arrivals",
        {"uri": _uri("arrivals") + "/", "scan": True},
        {"tables": ["DAILY", "HOURLY"]},
        ["DAILY", "HOURLY"],
        # Tight grace so the cron-driven arrivals visibly keep it fresh (and stop → it alerts).
        _policy(1, 2, 6),
    ),
]


def _req(method: str, base: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"content-type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req) as r:  # noqa: S310
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        print(f"  ! {method} {path} -> {e.code}: {e.read().decode()[:300]}")
        return {}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=DEFAULT_BASE, help="API base, e.g. http://127.0.0.1:8000/api/v1")
    ap.add_argument("--no-profile", action="store_true", help="create monitors but don't profile")
    args = ap.parse_args()

    print(f"Seeding file monitors against {args.base}\n  bucket s3://{BUCKET}/{PREFIX}/\n")
    created = 0
    for name, coords, scope, refs, policy in SCENARIOS:
        conn = _req("POST", args.base, "/connections", {
            "source_type": "s3", "name": name, "auth_method": "none", "coordinates": coords,
        }).get("data")
        if not conn:
            print(f"  ✗ {name}: connection failed")
            continue
        mon = _req("POST", args.base, "/monitors", {
            "connection_id": conn["id"], "name": name, "scope": scope, "alert_policy": policy,
        }).get("data")
        if not mon:
            print(f"  ✗ {name}: monitor failed")
            continue
        created += 1
        print(f"  ✓ {name}  (monitor {mon['id']})")
        if args.no_profile:
            continue
        for ref in refs:
            res = _req("POST", args.base, f"/monitors/{mon['id']}/tables/{ref}:profile")
            if res.get("data"):
                pillars = res["data"].get("pillars_recorded") or []
                print(f"      profiled {ref}: {', '.join(pillars) or 'no pillars'}")
            else:
                print(f"      profiled {ref}: FAILED (does the server have S3 creds + boto3?)")

    print(f"\nDone — {created}/{len(SCENARIOS)} monitors created.")
    print("Open the UI → Observability to see freshness (from file arrival), volume, schema, quality.")
    print("Run scripts/drop_arrival.py (or install scripts/file_arrival.crontab) to keep the "
          "'Scheduled arrivals' monitor fresh.")


if __name__ == "__main__":
    main()
