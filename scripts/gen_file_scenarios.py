#!/usr/bin/env python3
"""Generate a complete set of FILE-source test scenarios for data observability.

This is the single source of truth for the file test-data kit. It writes a deterministic
tree of real files covering every format and layout the FileConnector supports, so the same
set can be (a) read by the pytest suite, (b) uploaded to S3 by seed_s3_file_scenarios.py, and
(c) pointed at by monitors created with seed_file_monitors.py.

Scenario matrix
---------------
  single/        one file -> one table, one per supported format:
    orders.csv         CSV, typed cols + nulls (quality) + ORDER_TS (watermark candidate)
    customers.tsv      TSV (tab-delimited)
    events.ndjson      newline-delimited JSON
    products.json      JSON array
    inventory.json     JSON {"records": [...]} wrapper
  lake/          scan-prefix root (scan:true) -> many tables discovered one level below:
    regions.csv        a file  -> REGIONS table
    shipments/         a folder of part files -> SHIPMENTS table (rows unioned)
      part-0000.csv, part-0001.csv, _SUCCESS (marker, ignored)
    payments/          a folder of part files -> PAYMENTS table
      part-0000.ndjson, part-0001.ndjson, .hidden.csv (hidden, ignored)
  typed/
    types_sample.csv   one column per inferred type (NUMBER/float/BOOLEAN/DATE/TIMESTAMP/VARCHAR)
    with_nulls.csv     a ~50%-null column -> low quality score
  parquet/
    sales.parquet      Parquet (needs pyarrow; skipped with a note if unavailable)
  arrivals/daily/
    load_<date>.csv    freshness: a seed arrival; drop_arrival.py adds more on a schedule

Usage::

    python scripts/gen_file_scenarios.py [--root testdata/file_scenarios] [--clean]

Deterministic (fixed seed) so re-running produces identical bytes — safe to re-seed S3.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import shutil
from datetime import date, datetime, timedelta

SEED = 42
BASE_DAY = date(2026, 10, 1)  # fixed so generated data is reproducible

STATUSES = ["PLACED", "PAID", "SHIPPED", "DELIVERED", "CANCELLED"]
COUNTRIES = ["US", "CA", "GB", "DE", "IN", "JP", "AU", "BR"]
EVENT_TYPES = ["login", "page_view", "add_to_cart", "checkout", "search"]
REGIONS = ["AMER", "EMEA", "APAC"]


# ----------------------------- writers -----------------------------
def _ts(day_offset: int, rng: random.Random) -> str:
    d = BASE_DAY + timedelta(days=day_offset)
    return f"{d.isoformat()} {rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:00"


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as fh:
        fh.write(text)
    print(f"  wrote {path}")


def _csv(rows: list[dict], header: list[str], delimiter: str = ",") -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, delimiter=delimiter)
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in header})
    return buf.getvalue()


def _ndjson(rows: list[dict]) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


# ----------------------------- datasets -----------------------------
def gen_orders(rng: random.Random, n: int = 25) -> str:
    rows = []
    for i in range(1, n + 1):
        rows.append(
            {
                "ORDER_ID": i,
                "CUSTOMER_ID": rng.randint(1000, 1099),
                # ~1 in 6 amounts is null -> drives the quality score below 100.
                "AMOUNT": None if i % 6 == 0 else round(rng.uniform(5, 500), 2),
                "STATUS": rng.choice(STATUSES),
                "ORDER_TS": _ts(i % 10, rng),
            }
        )
    return _csv(rows, ["ORDER_ID", "CUSTOMER_ID", "AMOUNT", "STATUS", "ORDER_TS"])


def gen_customers_tsv(rng: random.Random, n: int = 20) -> str:
    rows = []
    for i in range(1, n + 1):
        rows.append(
            {
                "CUSTOMER_ID": 1000 + i,
                "NAME": f"Customer {i}",
                "EMAIL": None if i % 5 == 0 else f"user{i}@example.com",
                "COUNTRY": rng.choice(COUNTRIES),
                "SIGNED_UP": (BASE_DAY - timedelta(days=rng.randint(0, 300))).isoformat(),
            }
        )
    return _csv(rows, ["CUSTOMER_ID", "NAME", "EMAIL", "COUNTRY", "SIGNED_UP"], delimiter="\t")


def gen_events_ndjson(rng: random.Random, n: int = 30) -> str:
    rows = [
        {
            "EVENT_ID": i,
            "USER_ID": rng.randint(1000, 1099),
            "EVENT_TYPE": rng.choice(EVENT_TYPES),
            "EVENT_TS": _ts(i % 10, rng),
            "VALUE": round(rng.uniform(0, 1), 4),
        }
        for i in range(1, n + 1)
    ]
    return _ndjson(rows)


def gen_products_json(rng: random.Random, n: int = 15) -> str:
    rows = [
        {
            "PRODUCT_ID": i,
            "NAME": f"Product {i}",
            "PRICE": round(rng.uniform(1, 999), 2),
            "IN_STOCK": bool(i % 2),
        }
        for i in range(1, n + 1)
    ]
    return json.dumps(rows, indent=2)


def gen_inventory_json(rng: random.Random, n: int = 12) -> str:
    records = [
        {"SKU": f"SKU-{i:04d}", "WAREHOUSE": rng.choice(["EAST", "WEST"]), "QTY": rng.randint(0, 500)}
        for i in range(1, n + 1)
    ]
    return json.dumps({"records": records}, indent=2)


def gen_regions_csv() -> str:
    rows = [{"REGION_CODE": r, "REGION_NAME": r.title()} for r in REGIONS]
    return _csv(rows, ["REGION_CODE", "REGION_NAME"])


def gen_shipment_part(rng: random.Random, start: int, n: int) -> str:
    rows = [
        {
            "SHIPMENT_ID": i,
            "ORDER_ID": rng.randint(1, 25),
            "CARRIER": rng.choice(["UPS", "FEDEX", "DHL"]),
            "SHIPPED_TS": _ts(i % 10, rng),
        }
        for i in range(start, start + n)
    ]
    return _csv(rows, ["SHIPMENT_ID", "ORDER_ID", "CARRIER", "SHIPPED_TS"])


def gen_payment_part(rng: random.Random, start: int, n: int) -> str:
    rows = [
        {
            "PAYMENT_ID": i,
            "ORDER_ID": rng.randint(1, 25),
            "METHOD": rng.choice(["card", "paypal", "wire"]),
            "AMOUNT": round(rng.uniform(5, 500), 2),
            "PAID_TS": _ts(i % 10, rng),
        }
        for i in range(start, start + n)
    ]
    return _ndjson(rows)


def gen_types_sample() -> str:
    rows = [
        {
            "N_INT": i,
            "F_FLOAT": round(i * 1.5, 2),
            "B_BOOL": bool(i % 2),
            "D_DATE": (BASE_DAY + timedelta(days=i)).isoformat(),
            "T_TIMESTAMP": f"{(BASE_DAY + timedelta(days=i)).isoformat()} 08:30:00",
            "S_STRING": f"label-{i}",
        }
        for i in range(1, 11)
    ]
    return _csv(rows, ["N_INT", "F_FLOAT", "B_BOOL", "D_DATE", "T_TIMESTAMP", "S_STRING"])


def gen_with_nulls() -> str:
    # VAL is null on every other row -> ~50% null rate -> a visibly low quality score.
    rows = [{"ID": i, "VAL": None if i % 2 == 0 else i * 10} for i in range(1, 21)]
    return _csv(rows, ["ID", "VAL"])


def gen_arrival_csv(rng: random.Random, when: date, rows: int = 10) -> str:
    data = [
        {"ROW_ID": i, "LOAD_DATE": when.isoformat(), "VALUE": rng.randint(1, 1000)}
        for i in range(1, rows + 1)
    ]
    return _csv(data, ["ROW_ID", "LOAD_DATE", "VALUE"])


# ----------------------------- orchestration -----------------------------
def generate(root: str, clean: bool = False) -> dict:
    """Write the whole tree under `root`. Returns a manifest of relative paths written."""
    if clean and os.path.isdir(root):
        shutil.rmtree(root)
    rng = random.Random(SEED)

    _write(os.path.join(root, "single/orders.csv"), gen_orders(rng))
    _write(os.path.join(root, "single/customers.tsv"), gen_customers_tsv(rng))
    _write(os.path.join(root, "single/events.ndjson"), gen_events_ndjson(rng))
    _write(os.path.join(root, "single/products.json"), gen_products_json(rng))
    _write(os.path.join(root, "single/inventory.json"), gen_inventory_json(rng))

    _write(os.path.join(root, "lake/regions.csv"), gen_regions_csv())
    _write(os.path.join(root, "lake/shipments/part-0000.csv"), gen_shipment_part(rng, 1, 8))
    _write(os.path.join(root, "lake/shipments/part-0001.csv"), gen_shipment_part(rng, 9, 8))
    _write(os.path.join(root, "lake/shipments/_SUCCESS"), "")  # Spark marker — must be ignored
    _write(os.path.join(root, "lake/payments/part-0000.ndjson"), gen_payment_part(rng, 1, 7))
    _write(os.path.join(root, "lake/payments/part-0001.ndjson"), gen_payment_part(rng, 8, 7))
    _write(os.path.join(root, "lake/payments/.hidden.csv"), "x\n1\n")  # hidden — must be ignored

    _write(os.path.join(root, "typed/types_sample.csv"), gen_types_sample())
    _write(os.path.join(root, "typed/with_nulls.csv"), gen_with_nulls())

    _write(os.path.join(root, "arrivals/daily/load_2026-10-01.csv"), gen_arrival_csv(rng, BASE_DAY))

    manifest = {"root": root, "parquet": _maybe_parquet(root, rng)}
    return manifest


def _maybe_parquet(root: str, rng: random.Random) -> bool:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except Exception:
        print("  ! pyarrow not installed — skipping parquet/sales.parquet "
              "(install pyarrow to include it)")
        return False
    n = 20
    table = pa.table(
        {
            "SALE_ID": list(range(1, n + 1)),
            "REGION": [rng.choice(REGIONS) for _ in range(n)],
            "AMOUNT": [round(rng.uniform(10, 1000), 2) for _ in range(n)],
            "SALE_TS": [
                datetime.fromisoformat(f"{(BASE_DAY + timedelta(days=i % 10)).isoformat()}T09:00:00")
                for i in range(n)
            ],
        }
    )
    path = os.path.join(root, "parquet/sales.parquet")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pq.write_table(table, path)
    print(f"  wrote {path}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="testdata/file_scenarios", help="output directory")
    ap.add_argument("--clean", action="store_true", help="remove the root first")
    args = ap.parse_args()
    print(f"Generating file scenarios under {args.root} …")
    manifest = generate(args.root, clean=args.clean)
    print(f"Done. parquet_included={manifest['parquet']}")


if __name__ == "__main__":
    main()
