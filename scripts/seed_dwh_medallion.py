#!/usr/bin/env python3
"""Seed DWH_TEST across the full medallion — DMS_BRONZE, DMS_SILVER, DMS_GOLD — with a
comprehensive, scenario-complete demo set for the Data Observability platform.

WHAT IT COVERS
--------------
Every load pattern the observability classifier recognises *and* every pillar scenario the
UI needs to show something interesting, spread over the last ``SEED_DAYS`` days (default 15):

  Load patterns      APPEND · CDC/UPSERT · SCD2 · SNAPSHOT (truncate&load) · PARTITIONED · REFERENCE
  Freshness          healthy (recent LOAD_TS) on most tables; one deliberately STALE table whose
                     last load is ~8 days old  -> freshness incident
  Volume             organic daily growth; a sharp DIP on one day and a SPIKE on another -> anomalies
  Schema             a table that gains a column partway through the window -> schema/distribution drift
  Data quality       null bursts, duplicate natural keys, out-of-range / negative values,
                     a late-appearing enum value, and a referential-integrity break (orphan FK)

MEDALLION SEMANTICS
-------------------
  BRONZE   raw ingestion, diverse load patterns, intentionally messy (the quality issues live here)
  SILVER   conformed/cleaned facts + dimensions (mostly healthy; one controlled orphan-FK break)
  GOLD     business marts — small daily aggregates / KPIs / 360 tables (one row per day / entity)

CONNECTION (key-pair, same proven path as seed_dwh_bronze.py). Pass creds via env:
  SF_ACCOUNT   e.g. kea72530.us-east-1
  SF_USER      e.g. DHIRAJ_DESKTOP
  SF_KEY       path to the PKCS8 private key (.p8)
  SF_KEY_PWD   key passphrase (omit/empty if the key is unencrypted)
  SF_WH        warehouse (default SNOWFLAKE_LEARNING_WH)
  SF_ROLE      role WITH CREATE TABLE on the DMS_* schemas (default DATA_READ_WRITE)
  SEED_DAYS    days of history to generate (default 15)
  SEED_LAYERS  comma list to limit layers, e.g. "bronze,gold" (default "bronze,silver,gold")

Idempotent: every table is CREATE OR REPLACE, so re-running yields a clean, deterministic set
(random.seed is fixed). Safe to run repeatedly.
"""
from __future__ import annotations

import datetime as dt
import os
import random
import re

import snowflake.connector as sc

DB = "DWH_TEST"
BRONZE, SILVER, GOLD = "DMS_BRONZE", "DMS_SILVER", "DMS_GOLD"
DAYS = int(os.environ.get("SEED_DAYS", "15"))
LAYERS = {s.strip().lower() for s in os.environ.get("SEED_LAYERS", "bronze,silver,gold").split(",")}
random.seed(42)

TODAY = dt.date.today()
START = TODAY - dt.timedelta(days=DAYS - 1)
DAYLIST = [START + dt.timedelta(days=i) for i in range(DAYS)]
# Anomaly days (indices into DAYLIST), scaled to the window so they always land inside it.
DIP_DAY = max(1, DAYS - 5)      # a visible volume drop -> volume incident
SPIKE_DAY = max(1, DAYS - 9)    # a volume surge
DRIFT_DAY = max(1, DAYS // 2)   # schema/distribution change starts here
STALE_CUTOFF_DAYS = 8           # the "legacy feed" stops loading this many days ago

CITIES = ["New York", "Chicago", "Austin", "Seattle", "Denver", "Miami", "Boston", "Portland"]
SEGMENTS = ["SMB", "Mid-Market", "Enterprise", "Startup"]
STATUSES = ["PLACED", "PAID", "SHIPPED", "DELIVERED", "CANCELLED"]
EVENT_TYPES = ["login", "page_view", "add_to_cart", "checkout", "search", "logout"]
CATEGORIES = ["Electronics", "Home", "Apparel", "Grocery", "Toys", "Sports"]
CHANNELS = ["web", "mobile", "partner", "store"]
COUNTRIES = [
    ("US", "United States", "AMER", "USD"), ("CA", "Canada", "AMER", "CAD"),
    ("GB", "United Kingdom", "EMEA", "GBP"), ("DE", "Germany", "EMEA", "EUR"),
    ("FR", "France", "EMEA", "EUR"), ("IN", "India", "APAC", "INR"),
    ("JP", "Japan", "APAC", "JPY"), ("AU", "Australia", "APAC", "AUD"),
    ("BR", "Brazil", "AMER", "BRL"), ("SG", "Singapore", "APAC", "SGD"),
]
REGIONS = ["AMER", "EMEA", "APAC"]
N_CUSTOMERS = 2000
N_PRODUCTS = 400


# ----------------------------------------------------------------------------- helpers
def ts(day: dt.date) -> str:
    """A timestamp inside the given day (NTZ string)."""
    return f"{day.isoformat()} {random.randint(0,23):02d}:{random.randint(0,59):02d}:{random.randint(0,59):02d}"


def grow(base: int, i: int, *, dip=True, spike=True) -> int:
    """Organic ~1-3%/day growth with an optional dip + spike for interesting trend charts."""
    n = base * (1.0 + 0.015 * i) * random.uniform(0.96, 1.05)
    if dip and i == DIP_DAY:
        n *= 0.40       # sharp drop -> volume anomaly / incident
    if spike and i == SPIKE_DAY:
        n *= 1.85       # surge
    return max(1, int(n))


def ddl(cur, sql: str) -> None:
    cur.execute(sql)


# Tables that already exist and are owned by another role (we can't CREATE OR REPLACE them,
# but DATA_READ_WRITE can TRUNCATE + INSERT). Maps (schema, table) -> [(col_name, max_str_len)].
_REFRESH: dict[tuple[str, str], list[tuple[str, int | None]]] = {}


def _existing_columns(cur, schema: str, table: str) -> list[tuple[str, int | None]]:
    cur.execute(f'DESCRIBE TABLE {DB}.{schema}."{table}"')
    out = []
    for r in cur.fetchall():
        name, dtype = r[0], (r[1] or "").upper()
        m = re.search(r"(?:VARCHAR|CHAR|STRING|TEXT)\((\d+)\)", dtype)
        out.append((name, int(m.group(1)) if m else None))
    return out


def create(cur, schema: str, table: str, coldefs: str) -> None:
    """Prefer CREATE OR REPLACE (gets our exact schema). If the table pre-exists and is owned by
    another role, fall back to refresh-in-place: keep its schema, TRUNCATE, and INSERT the columns
    our data shares with it."""
    try:
        ddl(cur, f'CREATE OR REPLACE TABLE {DB}.{schema}."{table}" ({coldefs})')
        _REFRESH.pop((schema, table), None)
    except sc.errors.ProgrammingError as e:
        if "OWNERSHIP" not in str(e) and "Insufficient privileges" not in str(e):
            raise
        # Owned elsewhere — refresh in place (TRUNCATE + INSERT into the intersection of columns).
        cur.execute(f'CREATE TABLE IF NOT EXISTS {DB}.{schema}."{table}" ({coldefs})')
        cur.execute(f'TRUNCATE TABLE {DB}.{schema}."{table}"')
        _REFRESH[(schema, table)] = _existing_columns(cur, schema, table)


def insert_many(cur, schema: str, table: str, cols: list[str], rows: list[tuple], chunk=16000) -> None:
    if not rows:
        return
    existing = _REFRESH.get((schema, table))
    if existing is not None:
        # Align our positional rows to the existing table's columns (by name); drop extras, leave
        # missing columns NULL; truncate strings that exceed the legacy column width.
        emap = dict(existing)
        keep = [c for c in cols if c in emap]
        idx = [cols.index(c) for c in keep]
        lens = [emap[c] for c in keep]
        cols = keep
        new_rows = []
        for r in rows:
            vals = [r[i] for i in idx]
            for k, ln in enumerate(lens):
                if ln is not None and isinstance(vals[k], str) and len(vals[k]) > ln:
                    vals[k] = vals[k][:ln]
            new_rows.append(tuple(vals))
        rows = new_rows
        if not cols:
            return
    ph = ", ".join(["%s"] * len(cols))
    sql = f'INSERT INTO {DB}.{schema}."{table}" ({", ".join(cols)}) VALUES ({ph})'
    for j in range(0, len(rows), chunk):
        cur.executemany(sql, rows[j:j + chunk])


# ============================================================================= BRONZE
def bronze_orders_cdc(cur):
    """CDC/UPSERT + quality issues: negative AMOUNT, null STATUS, a late enum value."""
    t = "BRZ_ORDERS"
    create(cur, BRONZE, t,
           "ORDER_ID NUMBER, CUSTOMER_ID NUMBER, PRODUCT_ID NUMBER, AMOUNT NUMBER(12,2), "
           "STATUS STRING, CHANNEL STRING, CREATED_AT TIMESTAMP_NTZ, UPDATED_AT TIMESTAMP_NTZ, "
           "OP STRING, IS_DELETED BOOLEAN, LOAD_TS TIMESTAMP_NTZ")
    cols = ["ORDER_ID", "CUSTOMER_ID", "PRODUCT_ID", "AMOUNT", "STATUS", "CHANNEL",
            "CREATED_AT", "UPDATED_AT", "OP", "IS_DELETED", "LOAD_TS"]
    rows: list[tuple] = []
    oid = 1
    live: list[int] = []
    for i, day in enumerate(DAYLIST):
        for _ in range(grow(350, i)):
            amount = round(random.uniform(10, 2000), 2)
            if random.random() < 0.01:               # quality: out-of-range negative amount
                amount = -amount
            status = "PLACED"
            if random.random() < 0.02:               # quality: null status burst
                status = None
            rows.append((oid, random.randint(1, N_CUSTOMERS), random.randint(1, N_PRODUCTS), amount,
                         status, random.choice(CHANNELS), ts(day), ts(day), "I", False, ts(day)))
            live.append(oid)
            oid += 1
        for _ in range(min(int(grow(350, i) * 0.5), len(live))):   # in-place updates -> advance UPDATED_AT
            o = random.choice(live)
            st = random.choice(STATUSES)
            if i >= DRIFT_DAY and random.random() < 0.05:
                st = "RETURNED"                       # quality: a NEW enum value appears late -> drift
            deleted = st == "CANCELLED"
            rows.append((o, random.randint(1, N_CUSTOMERS), random.randint(1, N_PRODUCTS),
                         round(random.uniform(10, 2000), 2), st, random.choice(CHANNELS),
                         ts(day), ts(day), "D" if deleted else "U", deleted, ts(day)))
    insert_many(cur, BRONZE, t, cols, rows)
    return t, len(rows)


def bronze_events_append(cur):
    """APPEND event stream; organic growth + dip + spike -> volume anomalies."""
    t = "BRZ_EVENTS"
    create(cur, BRONZE, t,
           "EVENT_ID NUMBER, CUSTOMER_ID NUMBER, SESSION_ID NUMBER, EVENT_TYPE STRING, "
           "EVENT_TS TIMESTAMP_NTZ, LOAD_TS TIMESTAMP_NTZ, BATCH_ID NUMBER")
    cols = ["EVENT_ID", "CUSTOMER_ID", "SESSION_ID", "EVENT_TYPE", "EVENT_TS", "LOAD_TS", "BATCH_ID"]
    rows, eid = [], 1
    for i, day in enumerate(DAYLIST):
        for _ in range(grow(2200, i)):
            rows.append((eid, random.randint(1, N_CUSTOMERS), random.randint(1, 40000),
                         random.choice(EVENT_TYPES), ts(day), ts(day), i + 1))
            eid += 1
    insert_many(cur, BRONZE, t, cols, rows)
    return t, len(rows)


def bronze_customers_snapshot(cur):
    """Daily full SNAPSHOT of the customer master; quality: ~5% null EMAIL + a few duplicate IDs."""
    t = "BRZ_CUSTOMERS"
    create(cur, BRONZE, t,
           "CUSTOMER_ID NUMBER, FULL_NAME STRING, EMAIL STRING, CITY STRING, SEGMENT STRING, "
           "COUNTRY_CODE STRING, SIGNUP_DATE DATE, INGESTED_AT TIMESTAMP_NTZ")
    cols = ["CUSTOMER_ID", "FULL_NAME", "EMAIL", "CITY", "SEGMENT", "COUNTRY_CODE", "SIGNUP_DATE", "INGESTED_AT"]
    rows = []
    for c in range(1, N_CUSTOMERS + 1):
        email = None if random.random() < 0.05 else f"user{c}@example.com"   # quality: null rate
        rows.append((c, f"Customer {c}", email, random.choice(CITIES), random.choice(SEGMENTS),
                     random.choice(COUNTRIES)[0], (START - dt.timedelta(days=random.randint(0, 900))),
                     ts(TODAY)))
    for _ in range(12):                                                      # quality: duplicate keys
        d = random.randint(1, N_CUSTOMERS)
        rows.append((d, f"Customer {d}", f"dup{d}@example.com", random.choice(CITIES),
                     random.choice(SEGMENTS), "US", START, ts(TODAY)))
    insert_many(cur, BRONZE, t, cols, rows)
    return t, len(rows)


def bronze_products_reference(cur):
    """REFERENCE / lookup (small, no watermark)."""
    t = "BRZ_PRODUCTS"
    create(cur, BRONZE, t,
           "PRODUCT_ID NUMBER, PRODUCT_NAME STRING, CATEGORY STRING, UNIT_PRICE NUMBER(10,2), ACTIVE BOOLEAN")
    rows = [(p, f"Product {p}", random.choice(CATEGORIES), round(random.uniform(5, 500), 2),
             random.random() > 0.1) for p in range(1, N_PRODUCTS + 1)]
    insert_many(cur, BRONZE, t, ["PRODUCT_ID", "PRODUCT_NAME", "CATEGORY", "UNIT_PRICE", "ACTIVE"], rows)
    return t, len(rows)


def bronze_pageviews_partitioned(cur):
    """PARTITIONED by PARTITION_DATE; a DEVICE_TYPE column that only populates from DRIFT_DAY on."""
    t = "BRZ_PAGEVIEWS"
    create(cur, BRONZE, t,
           "PAGEVIEW_ID NUMBER, SESSION_ID NUMBER, URL STRING, DEVICE_TYPE STRING, "
           "VIEW_TS TIMESTAMP_NTZ, PARTITION_DATE DATE, LOAD_TS TIMESTAMP_NTZ")
    urls = ["/", "/products", "/pricing", "/cart", "/checkout", "/account", "/search", "/blog"]
    cols = ["PAGEVIEW_ID", "SESSION_ID", "URL", "DEVICE_TYPE", "VIEW_TS", "PARTITION_DATE", "LOAD_TS"]
    rows, pid = [], 1
    for i, day in enumerate(DAYLIST):
        for _ in range(grow(1800, i)):
            device = random.choice(["desktop", "mobile", "tablet"]) if i >= DRIFT_DAY else None  # drift
            rows.append((pid, random.randint(1, 40000), random.choice(urls), device, ts(day), day, ts(day)))
            pid += 1
    insert_many(cur, BRONZE, t, cols, rows)
    return t, len(rows)


def bronze_inventory_snapshot(cur):
    """SNAPSHOT (truncate & load): NO *_ts/*_at/*_date column -> classifier returns SNAPSHOT."""
    t = "BRZ_INVENTORY"
    create(cur, BRONZE, t,
           "SKU STRING, WAREHOUSE STRING, ON_HAND NUMBER, RESERVED NUMBER, REORDER_POINT NUMBER, REGION STRING")
    rows = []
    for p in range(1, N_PRODUCTS + 1):
        for wh in ("WH-EAST", "WH-WEST", "WH-CENTRAL"):
            rows.append((f"SKU-{p:04d}", wh, random.randint(0, 5000), random.randint(0, 500),
                         random.randint(10, 200), random.choice(REGIONS)))
    insert_many(cur, BRONZE, t, ["SKU", "WAREHOUSE", "ON_HAND", "RESERVED", "REORDER_POINT", "REGION"], rows)
    return t, len(rows)


def bronze_legacy_feed_stale(cur):
    """APPEND feed that STOPPED loading STALE_CUTOFF_DAYS ago -> freshness incident."""
    t = "BRZ_LEGACY_FEED"
    create(cur, BRONZE, t,
           "RECORD_ID NUMBER, SOURCE_SYSTEM STRING, PAYLOAD STRING, LOAD_TS TIMESTAMP_NTZ")
    cutoff = TODAY - dt.timedelta(days=STALE_CUTOFF_DAYS)
    rows, rid = [], 1
    for i, day in enumerate(DAYLIST):
        if day > cutoff:
            continue                       # nothing recent -> stale
        for _ in range(grow(300, i, dip=False, spike=False)):
            rows.append((rid, random.choice(["MAINFRAME", "AS400", "FLATFILE"]),
                         f"payload-{rid}", ts(day)))
            rid += 1
    insert_many(cur, BRONZE, t, ["RECORD_ID", "SOURCE_SYSTEM", "PAYLOAD", "LOAD_TS"], rows)
    return t, len(rows)


def bronze_customer_scd2(cur):
    """SCD2 raw dimension with effective-dating + version."""
    t = "BRZ_CUSTOMER_SCD2"
    create(cur, BRONZE, t,
           "CUSTOMER_SK NUMBER, CUSTOMER_ID NUMBER, NAME STRING, CITY STRING, SEGMENT STRING, "
           "EFFECTIVE_FROM DATE, EFFECTIVE_TO DATE, IS_CURRENT BOOLEAN, SCD_VERSION NUMBER, LOAD_TS TIMESTAMP_NTZ")
    cols = ["CUSTOMER_SK", "CUSTOMER_ID", "NAME", "CITY", "SEGMENT", "EFFECTIVE_FROM",
            "EFFECTIVE_TO", "IS_CURRENT", "SCD_VERSION", "LOAD_TS"]
    N = 800
    rows, sk, current = [], 1, {}
    for c in range(1, N + 1):
        city, seg = random.choice(CITIES), random.choice(SEGMENTS)
        rows.append([sk, c, f"Customer {c}", city, seg, START, None, True, 1, ts(START)])
        current[c] = len(rows) - 1
        sk += 1
    for _i, day in enumerate(DAYLIST[1:], start=1):
        for _ in range(max(3, int(N * 0.012))):
            c = random.randint(1, N)
            idx = current[c]
            ver = rows[idx][8]
            rows[idx][6], rows[idx][7] = day, False          # close prior version
            rows.append([sk, c, f"Customer {c}", random.choice(CITIES), random.choice(SEGMENTS),
                         day, None, True, ver + 1, ts(day)])
            current[c] = len(rows) - 1
            sk += 1
    insert_many(cur, BRONZE, t, cols, [tuple(r) for r in rows])
    return t, len(rows)


BRONZE_PLAN = [
    ("CDC/UPSERT", bronze_orders_cdc), ("APPEND", bronze_events_append),
    ("SNAPSHOT", bronze_customers_snapshot), ("REFERENCE", bronze_products_reference),
    ("PARTITIONED", bronze_pageviews_partitioned), ("SNAPSHOT", bronze_inventory_snapshot),
    ("APPEND/STALE", bronze_legacy_feed_stale), ("SCD2", bronze_customer_scd2),
]


# ============================================================================= SILVER
def silver_fct_orders(cur):
    """Conformed, cleaned orders fact (APPEND/incremental). AMOUNT>0, FKs present. One controlled
    orphan-FK break (a few PRODUCT_KEY that don't exist in SLV_DIM_PRODUCT) for referential checks."""
    t = "SLV_FCT_ORDERS"
    create(cur, SILVER, t,
           "ORDER_KEY NUMBER, ORDER_ID NUMBER, CUSTOMER_KEY NUMBER, PRODUCT_KEY NUMBER, "
           "ORDER_DATE DATE, AMOUNT NUMBER(12,2), QUANTITY NUMBER, STATUS STRING, CHANNEL STRING, "
           "LOADED_AT TIMESTAMP_NTZ")
    cols = ["ORDER_KEY", "ORDER_ID", "CUSTOMER_KEY", "PRODUCT_KEY", "ORDER_DATE", "AMOUNT",
            "QUANTITY", "STATUS", "CHANNEL", "LOADED_AT"]
    rows, k = [], 1
    for i, day in enumerate(DAYLIST):
        for _ in range(grow(320, i)):
            product_key = random.randint(1, N_PRODUCTS)
            if random.random() < 0.004:
                product_key = N_PRODUCTS + random.randint(1, 50)   # orphan FK -> referential break
            rows.append((k, 100000 + k, random.randint(1, N_CUSTOMERS), product_key, day,
                         round(random.uniform(10, 2000), 2), random.randint(1, 8),
                         random.choice(STATUSES), random.choice(CHANNELS), ts(day)))
            k += 1
    insert_many(cur, SILVER, t, cols, rows)
    return t, len(rows)


def silver_fct_events(cur):
    """Deduplicated, conformed event fact (APPEND)."""
    t = "SLV_FCT_EVENTS"
    create(cur, SILVER, t,
           "EVENT_KEY NUMBER, CUSTOMER_KEY NUMBER, SESSION_KEY NUMBER, EVENT_TYPE STRING, "
           "EVENT_DATE DATE, EVENT_TS TIMESTAMP_NTZ, LOADED_AT TIMESTAMP_NTZ")
    cols = ["EVENT_KEY", "CUSTOMER_KEY", "SESSION_KEY", "EVENT_TYPE", "EVENT_DATE", "EVENT_TS", "LOADED_AT"]
    rows, k = [], 1
    for i, day in enumerate(DAYLIST):
        for _ in range(grow(1900, i)):
            rows.append((k, random.randint(1, N_CUSTOMERS), random.randint(1, 35000),
                         random.choice(EVENT_TYPES), day, ts(day), ts(day)))
            k += 1
    insert_many(cur, SILVER, t, cols, rows)
    return t, len(rows)


def silver_dim_customer_scd2(cur):
    """Conformed SCD2 customer dimension."""
    t = "SLV_DIM_CUSTOMER"
    create(cur, SILVER, t,
           "CUSTOMER_KEY NUMBER, CUSTOMER_ID NUMBER, FULL_NAME STRING, EMAIL STRING, CITY STRING, "
           "SEGMENT STRING, COUNTRY_CODE STRING, EFFECTIVE_FROM DATE, EFFECTIVE_TO DATE, "
           "IS_CURRENT BOOLEAN, SCD_VERSION NUMBER, LOADED_AT TIMESTAMP_NTZ")
    cols = ["CUSTOMER_KEY", "CUSTOMER_ID", "FULL_NAME", "EMAIL", "CITY", "SEGMENT", "COUNTRY_CODE",
            "EFFECTIVE_FROM", "EFFECTIVE_TO", "IS_CURRENT", "SCD_VERSION", "LOADED_AT"]
    rows, k, current = [], 1, {}
    for c in range(1, N_CUSTOMERS + 1):
        rows.append([k, c, f"Customer {c}", f"user{c}@example.com", random.choice(CITIES),
                     random.choice(SEGMENTS), random.choice(COUNTRIES)[0], START, None, True, 1, ts(START)])
        current[c] = len(rows) - 1
        k += 1
    for _i, day in enumerate(DAYLIST[1:], start=1):
        for _ in range(max(5, int(N_CUSTOMERS * 0.01))):
            c = random.randint(1, N_CUSTOMERS)
            idx = current[c]
            ver = rows[idx][10]
            rows[idx][8], rows[idx][9] = day, False
            rows.append([k, c, f"Customer {c}", f"user{c}@example.com", random.choice(CITIES),
                         random.choice(SEGMENTS), random.choice(COUNTRIES)[0], day, None, True, ver + 1, ts(day)])
            current[c] = len(rows) - 1
            k += 1
    insert_many(cur, SILVER, t, cols, [tuple(r) for r in rows])
    return t, len(rows)


def silver_dim_product(cur):
    """SCD1 / current-state product dimension (SNAPSHOT)."""
    t = "SLV_DIM_PRODUCT"
    create(cur, SILVER, t,
           "PRODUCT_KEY NUMBER, PRODUCT_ID NUMBER, PRODUCT_NAME STRING, CATEGORY STRING, "
           "UNIT_PRICE NUMBER(10,2), ACTIVE BOOLEAN, UPDATED_AT TIMESTAMP_NTZ")
    rows = [(p, p, f"Product {p}", random.choice(CATEGORIES), round(random.uniform(5, 500), 2),
             random.random() > 0.1, ts(TODAY)) for p in range(1, N_PRODUCTS + 1)]
    insert_many(cur, SILVER, t,
                ["PRODUCT_KEY", "PRODUCT_ID", "PRODUCT_NAME", "CATEGORY", "UNIT_PRICE", "ACTIVE", "UPDATED_AT"], rows)
    return t, len(rows)


def silver_dim_date(cur):
    """REFERENCE date dimension spanning the window (+/- a margin)."""
    t = "SLV_DIM_DATE"
    create(cur, SILVER, t,
           "DATE_KEY NUMBER, CAL_DATE DATE, DAY_NAME STRING, MONTH_NAME STRING, "
           "YEAR NUMBER, QUARTER NUMBER, IS_WEEKEND BOOLEAN")
    rows = []
    for off in range(-10, DAYS + 10):
        d = START + dt.timedelta(days=off)
        rows.append((int(d.strftime("%Y%m%d")), d, d.strftime("%A"), d.strftime("%B"),
                     d.year, (d.month - 1) // 3 + 1, d.weekday() >= 5))
    insert_many(cur, SILVER, t,
                ["DATE_KEY", "CAL_DATE", "DAY_NAME", "MONTH_NAME", "YEAR", "QUARTER", "IS_WEEKEND"], rows)
    return t, len(rows)


SILVER_PLAN = [
    ("APPEND", silver_fct_orders), ("APPEND", silver_fct_events),
    ("SCD2", silver_dim_customer_scd2), ("SNAPSHOT", silver_dim_product),
    ("REFERENCE", silver_dim_date),
]


# ============================================================================= GOLD
def gold_kpi_daily(cur):
    """One row per day — headline KPIs. Revenue/orders dip on DIP_DAY (matches bronze) -> trend story."""
    t = "GLD_KPI_DAILY"
    create(cur, GOLD, t,
           "METRIC_DATE DATE, REVENUE NUMBER(14,2), ORDERS NUMBER, ACTIVE_USERS NUMBER, "
           "NEW_CUSTOMERS NUMBER, AVG_ORDER_VALUE NUMBER(10,2), CONVERSION_RATE NUMBER(6,4), LOADED_AT TIMESTAMP_NTZ")
    cols = ["METRIC_DATE", "REVENUE", "ORDERS", "ACTIVE_USERS", "NEW_CUSTOMERS",
            "AVG_ORDER_VALUE", "CONVERSION_RATE", "LOADED_AT"]
    rows = []
    for i, day in enumerate(DAYLIST):
        orders = grow(320, i)
        aov = round(random.uniform(60, 140), 2)
        rows.append((day, round(orders * aov, 2), orders, grow(5000, i), random.randint(20, 120),
                     aov, round(random.uniform(0.02, 0.06), 4), ts(day)))
    insert_many(cur, GOLD, t, cols, rows)
    return t, len(rows)


def gold_daily_sales_by_region(cur):
    """Day x region aggregate mart."""
    t = "GLD_DAILY_SALES_BY_REGION"
    create(cur, GOLD, t,
           "METRIC_DATE DATE, REGION STRING, REVENUE NUMBER(14,2), ORDERS NUMBER, "
           "UNITS NUMBER, RETURNING_RATE NUMBER(6,4), LOADED_AT TIMESTAMP_NTZ")
    cols = ["METRIC_DATE", "REGION", "REVENUE", "ORDERS", "UNITS", "RETURNING_RATE", "LOADED_AT"]
    rows = []
    for i, day in enumerate(DAYLIST):
        for region in REGIONS:
            orders = grow(110, i)
            rows.append((day, region, round(orders * random.uniform(70, 130), 2), orders,
                         orders * random.randint(1, 4), round(random.uniform(0.3, 0.6), 4), ts(day)))
    insert_many(cur, GOLD, t, cols, rows)
    return t, len(rows)


def gold_customer_360(cur):
    """One row per customer — lifetime roll-up (SNAPSHOT)."""
    t = "GLD_CUSTOMER_360"
    create(cur, GOLD, t,
           "CUSTOMER_ID NUMBER, SEGMENT STRING, LIFETIME_VALUE NUMBER(14,2), ORDERS_COUNT NUMBER, "
           "AVG_ORDER_VALUE NUMBER(10,2), FIRST_ORDER_DATE DATE, LAST_ORDER_DATE DATE, "
           "CHURN_RISK STRING, LOADED_AT TIMESTAMP_NTZ")
    cols = ["CUSTOMER_ID", "SEGMENT", "LIFETIME_VALUE", "ORDERS_COUNT", "AVG_ORDER_VALUE",
            "FIRST_ORDER_DATE", "LAST_ORDER_DATE", "CHURN_RISK", "LOADED_AT"]
    rows = []
    for c in range(1, N_CUSTOMERS + 1):
        oc = random.randint(0, 60)
        aov = round(random.uniform(40, 220), 2)
        last = TODAY - dt.timedelta(days=random.randint(0, 120))
        rows.append((c, random.choice(SEGMENTS), round(oc * aov, 2), oc, aov,
                     START - dt.timedelta(days=random.randint(30, 1000)), last,
                     random.choice(["low", "medium", "high"]), ts(TODAY)))
    insert_many(cur, GOLD, t, cols, rows)
    return t, len(rows)


def gold_product_performance_daily(cur):
    """Day x product performance (top sellers)."""
    t = "GLD_PRODUCT_PERFORMANCE_DAILY"
    create(cur, GOLD, t,
           "METRIC_DATE DATE, PRODUCT_ID NUMBER, CATEGORY STRING, UNITS_SOLD NUMBER, "
           "REVENUE NUMBER(14,2), RETURN_RATE NUMBER(6,4), LOADED_AT TIMESTAMP_NTZ")
    cols = ["METRIC_DATE", "PRODUCT_ID", "CATEGORY", "UNITS_SOLD", "REVENUE", "RETURN_RATE", "LOADED_AT"]
    rows = []
    top = list(range(1, 51))
    for _i, day in enumerate(DAYLIST):
        for p in top:
            units = random.randint(5, 400)
            rows.append((day, p, random.choice(CATEGORIES), units,
                         round(units * random.uniform(8, 120), 2), round(random.uniform(0, 0.15), 4), ts(day)))
    insert_many(cur, GOLD, t, cols, rows)
    return t, len(rows)


def gold_funnel_daily(cur):
    """Day-level conversion funnel."""
    t = "GLD_FUNNEL_DAILY"
    create(cur, GOLD, t,
           "METRIC_DATE DATE, VISITS NUMBER, PRODUCT_VIEWS NUMBER, ADD_TO_CART NUMBER, "
           "CHECKOUT NUMBER, PURCHASE NUMBER, CONVERSION_RATE NUMBER(6,4), LOADED_AT TIMESTAMP_NTZ")
    cols = ["METRIC_DATE", "VISITS", "PRODUCT_VIEWS", "ADD_TO_CART", "CHECKOUT", "PURCHASE",
            "CONVERSION_RATE", "LOADED_AT"]
    rows = []
    for i, day in enumerate(DAYLIST):
        visits = grow(8000, i)
        pv = int(visits * random.uniform(0.6, 0.8))
        atc = int(pv * random.uniform(0.2, 0.35))
        chk = int(atc * random.uniform(0.4, 0.6))
        pur = int(chk * random.uniform(0.5, 0.8))
        rows.append((day, visits, pv, atc, chk, pur, round(pur / visits, 4), ts(day)))
    insert_many(cur, GOLD, t, cols, rows)
    return t, len(rows)


GOLD_PLAN = [
    ("SNAPSHOT/DAILY", gold_kpi_daily), ("SNAPSHOT/DAILY", gold_daily_sales_by_region),
    ("SNAPSHOT", gold_customer_360), ("SNAPSHOT/DAILY", gold_product_performance_daily),
    ("SNAPSHOT/DAILY", gold_funnel_daily),
]


# ============================================================================= main
def connect():
    pwd = os.environ.get("SF_KEY_PWD") or ""
    kwargs = dict(
        account=os.environ["SF_ACCOUNT"], user=os.environ["SF_USER"],
        private_key_file=os.environ["SF_KEY"],
        warehouse=os.environ.get("SF_WH", "SNOWFLAKE_LEARNING_WH"), login_timeout=30,
    )
    if pwd:
        kwargs["private_key_file_pwd"] = pwd.encode()
    return sc.connect(**kwargs)


def main() -> None:
    con = connect()
    cur = con.cursor()
    role = os.environ.get("SF_ROLE", "DATA_READ_WRITE")
    cur.execute(f"USE ROLE {role}")
    cur.execute(f"USE WAREHOUSE {os.environ.get('SF_WH', 'SNOWFLAKE_LEARNING_WH')}")
    # Schemas already exist in this account; create defensively (no-op if present / insufficient priv ignored).
    for s in (BRONZE, SILVER, GOLD):
        try:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {DB}.{s}")
        except Exception:  # noqa: BLE001  — schema may be pre-owned; writing tables is what matters
            pass

    print(f"Seeding {DB} medallion — {DAYS} days ({START} .. {TODAY}) as role {role}")
    print(f"  anomalies: dip on {DAYLIST[DIP_DAY]}, spike on {DAYLIST[SPIKE_DAY]}, "
          f"schema drift from {DAYLIST[DRIFT_DAY]}, legacy feed stale since "
          f"{TODAY - dt.timedelta(days=STALE_CUTOFF_DAYS)}\n")

    layers = [("bronze", BRONZE, BRONZE_PLAN), ("silver", SILVER, SILVER_PLAN), ("gold", GOLD, GOLD_PLAN)]
    grand = 0
    for key, schema, plan in layers:
        if key not in LAYERS:
            continue
        print(f"== {DB}.{schema} ==")
        for pattern, fn in plan:
            try:
                name, n = fn(cur)
                grand += n
                print(f"  [{pattern:14}] {name:30} {n:>9,} rows")
            except Exception as e:  # noqa: BLE001 — one incompatible legacy table shouldn't abort the run
                print(f"  [{pattern:14}] {fn.__name__:30} SKIPPED: {str(e)[:70]}")
        con.commit()
        print()
    con.close()
    print(f"Done. {grand:,} rows across {', '.join(sorted(LAYERS))}.")


if __name__ == "__main__":
    main()
