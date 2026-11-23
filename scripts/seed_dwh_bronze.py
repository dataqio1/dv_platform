#!/usr/bin/env python3
"""Seed DWH_TEST.DMS_BRONZE with a comprehensive, load-pattern-diverse demo set.

Creates one representative table per load pattern the observability classifier recognises
(APPEND, CDC/UPSERT, SNAPSHOT/truncate-&-load, SCD2, PARTITIONED, REFERENCE) and loads
~DAYS days of realistic daily data so the UI trend charts (volume / freshness) look good.

Columns are chosen so each table AUTO-classifies to its intended pattern:
  APPEND       insert-only + time watermark (EVENT_TS / LOAD_TS), no update/op/scd/partition cols
  CDC/UPSERT   UPDATED_AT watermark + OP / IS_DELETED columns
  SCD2         EFFECTIVE_FROM / EFFECTIVE_TO / IS_CURRENT / SCD_VERSION
  SNAPSHOT     NO watermark candidate at all (no *_ts/*_at/*_date/timestamp cols) -> whole-table reload
  PARTITIONED  PARTITION_DATE partition column
  REFERENCE    small, no watermark, name hint 'REF_' -> lookup/reference

Connection: key-pair auth as DHIRAJ_DESKTOP (same proven path datadiff uses). Pass creds via env:
  SF_ACCOUNT, SF_USER, SF_KEY (path to .p8), SF_KEY_PWD (passphrase), SF_WH
Idempotent: every table is CREATE OR REPLACE, so re-running gives a clean, deterministic set.
"""
from __future__ import annotations
import os, random, datetime as dt
import snowflake.connector as sc

DB, SCHEMA = "DWH_TEST", "DMS_BRONZE"
DAYS = int(os.environ.get("SEED_DAYS", "30"))
random.seed(42)

TODAY = dt.date.today()
START = TODAY - dt.timedelta(days=DAYS - 1)
DAYLIST = [START + dt.timedelta(days=i) for i in range(DAYS)]

CITIES = ["New York", "Chicago", "Austin", "Seattle", "Denver", "Miami", "Boston", "Portland"]
SEGMENTS = ["SMB", "Mid-Market", "Enterprise", "Startup"]
STATUSES = ["PLACED", "PAID", "SHIPPED", "DELIVERED", "CANCELLED"]
EVENT_TYPES = ["login", "page_view", "add_to_cart", "checkout", "search", "logout"]
COUNTRIES = [
    ("US", "United States", "AMER", "USD"), ("CA", "Canada", "AMER", "CAD"),
    ("GB", "United Kingdom", "EMEA", "GBP"), ("DE", "Germany", "EMEA", "EUR"),
    ("FR", "France", "EMEA", "EUR"), ("IN", "India", "APAC", "INR"),
    ("JP", "Japan", "APAC", "JPY"), ("AU", "Australia", "APAC", "AUD"),
    ("BR", "Brazil", "AMER", "BRL"), ("SG", "Singapore", "APAC", "SGD"),
]


def ts(day: dt.date, within=True) -> str:
    """A timestamp inside the given day (NTZ string)."""
    h = random.randint(0, 23); m = random.randint(0, 59); s = random.randint(0, 59)
    return f"{day.isoformat()} {h:02d}:{m:02d}:{s:02d}"


def grow(base: int, i: int, bump_down_day: int | None = None) -> int:
    """Organic ~1-3%/day growth; optional volume-dip anomaly on one day for interesting charts."""
    n = int(base * (1.0 + 0.012 * i) * random.uniform(0.97, 1.05))
    if bump_down_day is not None and i == bump_down_day:
        n = int(n * 0.45)   # a visible dip (incident) on the trend chart
    return max(1, n)


def ddl(cur, sql: str):
    cur.execute(sql)


def insert_many(cur, table: str, cols: list[str], rows: list[tuple], chunk=10000):
    if not rows:
        return
    collist = ", ".join(cols)
    ph = ", ".join(["%s"] * len(cols))
    sql = f'INSERT INTO {DB}.{SCHEMA}."{table}" ({collist}) VALUES ({ph})'
    for j in range(0, len(rows), chunk):
        cur.executemany(sql, rows[j:j + chunk])


# ----------------------------------------------------------------------------- APPEND
def seed_append(cur):
    t = "BRZ_EVENTS"
    ddl(cur, f'CREATE OR REPLACE TABLE {DB}.{SCHEMA}."{t}" ('
             "EVENT_ID NUMBER, CUSTOMER_ID NUMBER, EVENT_TYPE STRING, "
             "EVENT_TS TIMESTAMP_NTZ, LOAD_TS TIMESTAMP_NTZ, BATCH_ID NUMBER)")
    rows = []; eid = 1
    for i, day in enumerate(DAYLIST):
        n = grow(1800, i, bump_down_day=19)
        for _ in range(n):
            rows.append((eid, random.randint(1, 5000), random.choice(EVENT_TYPES),
                         ts(day), ts(day), i + 1)); eid += 1
    insert_many(cur, t, ["EVENT_ID", "CUSTOMER_ID", "EVENT_TYPE", "EVENT_TS", "LOAD_TS", "BATCH_ID"], rows)
    return t, len(rows)


# ----------------------------------------------------------------------------- CDC / UPSERT
def seed_cdc(cur):
    t = "BRZ_ORDERS_CDC"
    ddl(cur, f'CREATE OR REPLACE TABLE {DB}.{SCHEMA}."{t}" ('
             "ORDER_ID NUMBER, CUSTOMER_ID NUMBER, PRODUCT_ID NUMBER, AMOUNT NUMBER(12,2), "
             "STATUS STRING, CREATED_AT TIMESTAMP_NTZ, UPDATED_AT TIMESTAMP_NTZ, "
             "OP STRING, IS_DELETED BOOLEAN, LOAD_TS TIMESTAMP_NTZ)")
    rows = []; oid = 1; live: list[tuple[int, dt.date]] = []
    for i, day in enumerate(DAYLIST):
        inserts = grow(400, i)
        for _ in range(inserts):
            rows.append((oid, random.randint(1, 5000), random.randint(1, 500),
                         round(random.uniform(10, 2000), 2), "PLACED",
                         ts(day), ts(day), "I", False, ts(day)))
            live.append((oid, day)); oid += 1
        # in-place updates to existing orders (status transitions) -> advance UPDATED_AT
        updates = int(inserts * 0.6)
        for _ in range(min(updates, len(live))):
            o, _d = random.choice(live)
            st = random.choice(STATUSES)
            deleted = st == "CANCELLED"
            rows.append((o, random.randint(1, 5000), random.randint(1, 500),
                         round(random.uniform(10, 2000), 2), st,
                         ts(day), ts(day), "D" if deleted else "U", deleted, ts(day)))
    insert_many(cur, t, ["ORDER_ID", "CUSTOMER_ID", "PRODUCT_ID", "AMOUNT", "STATUS",
                         "CREATED_AT", "UPDATED_AT", "OP", "IS_DELETED", "LOAD_TS"], rows)
    return t, len(rows)


# ----------------------------------------------------------------------------- SCD2
def seed_scd2(cur):
    t = "DIM_CUSTOMER_SCD2"
    ddl(cur, f'CREATE OR REPLACE TABLE {DB}.{SCHEMA}."{t}" ('
             "CUSTOMER_SK NUMBER, CUSTOMER_ID NUMBER, NAME STRING, CITY STRING, SEGMENT STRING, "
             "EFFECTIVE_FROM DATE, EFFECTIVE_TO DATE, IS_CURRENT BOOLEAN, SCD_VERSION NUMBER, "
             "LOAD_TS TIMESTAMP_NTZ)")
    N = 1200
    rows = []; sk = 1
    # current[cust] = (sk_index_in_rows, version, city, segment)
    current: dict[int, list] = {}
    for c in range(1, N + 1):
        city, seg = random.choice(CITIES), random.choice(SEGMENTS)
        rows.append([sk, c, f"Customer {c}", city, seg, START, None, True, 1, ts(START)])
        current[c] = [len(rows) - 1, 1, city, seg]; sk += 1
    # each day: a handful of customers change attributes -> close old version, append new
    for i, day in enumerate(DAYLIST[1:], start=1):
        changes = max(3, int(N * 0.01))
        for _ in range(changes):
            c = random.randint(1, N)
            idx, ver, city, seg = current[c]
            rows[idx][6] = day            # EFFECTIVE_TO on the superseded row
            rows[idx][7] = False          # IS_CURRENT = false
            ncity = random.choice(CITIES); nseg = random.choice(SEGMENTS)
            rows.append([sk, c, f"Customer {c}", ncity, nseg, day, None, True, ver + 1, ts(day)])
            current[c] = [len(rows) - 1, ver + 1, ncity, nseg]; sk += 1
    insert_many(cur, t, ["CUSTOMER_SK", "CUSTOMER_ID", "NAME", "CITY", "SEGMENT",
                         "EFFECTIVE_FROM", "EFFECTIVE_TO", "IS_CURRENT", "SCD_VERSION", "LOAD_TS"],
                [tuple(r) for r in rows])
    return t, len(rows)


# ----------------------------------------------------------------------------- SNAPSHOT (truncate & load)
def seed_snapshot(cur):
    # NO timestamp/date/_at/_ts watermark candidate anywhere -> classifier returns SNAPSHOT.
    t = "BRZ_ACCOUNT_BALANCES"
    ddl(cur, f'CREATE OR REPLACE TABLE {DB}.{SCHEMA}."{t}" ('
             "ACCOUNT_ID NUMBER, CUSTOMER_ID NUMBER, BALANCE NUMBER(14,2), "
             "ACCOUNT_STATUS STRING, REGION STRING, CURRENCY STRING)")
    rows = []
    for a in range(1, 4001):
        rows.append((a, random.randint(1, 5000), round(random.uniform(-500, 50000), 2),
                     random.choice(["ACTIVE", "DORMANT", "CLOSED", "FROZEN"]),
                     random.choice(["AMER", "EMEA", "APAC"]),
                     random.choice(["USD", "EUR", "GBP", "INR", "JPY"])))
    insert_many(cur, t, ["ACCOUNT_ID", "CUSTOMER_ID", "BALANCE", "ACCOUNT_STATUS", "REGION", "CURRENCY"], rows)
    return t, len(rows)


# ----------------------------------------------------------------------------- PARTITIONED
def seed_partitioned(cur):
    t = "BRZ_PAGEVIEWS_PART"
    ddl(cur, f'CREATE OR REPLACE TABLE {DB}.{SCHEMA}."{t}" ('
             "PAGEVIEW_ID NUMBER, SESSION_ID NUMBER, URL STRING, VIEW_TS TIMESTAMP_NTZ, "
             "PARTITION_DATE DATE, LOAD_TS TIMESTAMP_NTZ)")
    urls = ["/", "/products", "/pricing", "/cart", "/checkout", "/account", "/search", "/blog"]
    rows = []; pid = 1
    for i, day in enumerate(DAYLIST):
        n = grow(1500, i)
        for _ in range(n):
            rows.append((pid, random.randint(1, 20000), random.choice(urls),
                         ts(day), day, ts(day))); pid += 1
    insert_many(cur, t, ["PAGEVIEW_ID", "SESSION_ID", "URL", "VIEW_TS", "PARTITION_DATE", "LOAD_TS"], rows)
    return t, len(rows)


# ----------------------------------------------------------------------------- REFERENCE
def seed_reference(cur):
    t = "REF_COUNTRY"
    ddl(cur, f'CREATE OR REPLACE TABLE {DB}.{SCHEMA}."{t}" ('
             "COUNTRY_CODE STRING, COUNTRY_NAME STRING, REGION STRING, CURRENCY_CODE STRING)")
    insert_many(cur, t, ["COUNTRY_CODE", "COUNTRY_NAME", "REGION", "CURRENCY_CODE"], list(COUNTRIES))
    return t, len(COUNTRIES)


def main():
    con = sc.connect(
        account=os.environ["SF_ACCOUNT"], user=os.environ["SF_USER"],
        private_key_file=os.environ["SF_KEY"],
        private_key_file_pwd=os.environ["SF_KEY_PWD"].encode(),
        warehouse=os.environ.get("SF_WH", "COMPUTE_WH"), login_timeout=30,
    )
    cur = con.cursor()
    # DMS_BRONZE is owned by ACCOUNTADMIN; CREATE TABLE on it is granted to DATA_READ_WRITE.
    # SYSADMIN has USAGE (read) only. Role is overridable via SF_ROLE.
    cur.execute(f"USE ROLE {os.environ.get('SF_ROLE', 'DATA_READ_WRITE')}")
    print(f"Seeding {DB}.{SCHEMA} with {DAYS} days ({START} .. {TODAY})\n")
    plan = [("APPEND", seed_append), ("CDC/UPSERT", seed_cdc), ("SCD2", seed_scd2),
            ("SNAPSHOT", seed_snapshot), ("PARTITIONED", seed_partitioned), ("REFERENCE", seed_reference)]
    for pattern, fn in plan:
        name, n = fn(con.cursor() if False else cur)
        print(f"  [{pattern:11}] {name:22} {n:>8,} rows")
    con.commit(); con.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
