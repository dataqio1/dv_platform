"""Seed a last-30-days observability fixture on Snowflake across a source->curate->publish medallion.

Database DATA_OBSERVABILITY, schemas:
  data_observability_source   ORDERS_RAW         raw daily ingestion (messy: nulls, dupes, drift)
  data_observability_curate   ORDERS_CURATED     deduped + null-key dropped
  data_observability_publish  DAILY_ORDER_SUMMARY one metrics row per day (serving)

Dates are server-side (CURRENT_DATE) so it's always the real trailing 30 days. Injected anomalies
(by day-offset from today): volume SPIKE (-20), volume DROP (-10), MISSING day (-7), NULL-rate spike
(-3,-4,-5), category DRIFT (-0..-4), duplicate rows (-15).

Usage: PYTHONPATH=src DVP_PROFILE=local .venv/bin/python scripts/seed_observability_snowflake.py
"""
from __future__ import annotations
from dvp.abstractions.factory import build_container
from dvp.config import get_settings
from dvp.connectors.registry import build_connector
from dvp.db.models import Connection
from dvp.services.tenancy import bootstrap_local_tenant, resolve_context

SF_CONN = "3ba56de1-5484-49a6-8f65-a4a71d39babe"
DB = "DATA_OBSERVABILITY"
SRC = f"{DB}.DATA_OBSERVABILITY_SOURCE"
CUR = f"{DB}.DATA_OBSERVABILITY_CURATE"
PUB = f"{DB}.DATA_OBSERVABILITY_PUBLISH"

BASELINE = 4500
SPIKE_DAY, SPIKE_N = 20, 14000
DROP_DAY, DROP_N = 10, 800
MISSING_DAY = 7
NULL_DAYS = {3, 4, 5}
DRIFT_DAYS = {0, 1, 2, 3, 4}
DUP_DAY = 15

CATS = "ARRAY_CONSTRUCT('ELECTRONICS','CLOTHING','HOME','BOOKS','TOYS')[UNIFORM(0,4,RANDOM())]::string"


def conn():
    s = get_settings(); c = build_container(s); c.metadata.create_all()
    t = bootstrap_local_tenant(c.metadata, s)
    ctx = resolve_context(c.metadata, user_id=t.user_id, org_id=t.org_id, group_id=t.group_id)
    with c.metadata.session(ctx) as se:
        x = se.get(Connection, SF_CONN); st, co, am, ref = x.source_type, dict(x.coordinates), x.auth_method, x.secret_ref
    return build_connector(st, co, am, c.secrets.get(ctx, ref))


def day_count(d):
    if d == MISSING_DAY: return 0
    if d == SPIKE_DAY:   return SPIKE_N
    if d == DROP_DAY:    return DROP_N
    return BASELINE


def main():
    sf = conn()
    sf.execute(f"CREATE DATABASE IF NOT EXISTS {DB}")
    for sch in (SRC, CUR, PUB):
        sf.execute(f"CREATE SCHEMA IF NOT EXISTS {sch}")
    sf.execute(f"""CREATE OR REPLACE TABLE {SRC}.ORDERS_RAW (
        ORDER_ID NUMBER, EVENT_TS TIMESTAMP_NTZ, ORDER_DATE DATE, CUSTOMER_ID NUMBER,
        PRODUCT_CATEGORY STRING, AMOUNT NUMBER(10,2), STATUS STRING, REGION STRING, LOADED_AT TIMESTAMP_NTZ)""")

    offset = 1
    for d in range(30):
        cnt = day_count(d)
        if cnt == 0:
            print(f"  day -{d:<2}: MISSING (completeness gap)"); continue
        np = 35 if d in NULL_DAYS else 0
        cust = (f"CASE WHEN UNIFORM(0,100,RANDOM()) < {np} THEN NULL ELSE UNIFORM(1,50000,RANDOM()) END"
                if np else "UNIFORM(1,50000,RANDOM())")
        amt = (f"CASE WHEN UNIFORM(0,100,RANDOM()) < {np} THEN NULL ELSE ROUND(UNIFORM(500,50000,RANDOM())/100,2) END"
               if np else "ROUND(UNIFORM(500,50000,RANDOM())/100,2)")
        cat = (f"CASE WHEN UNIFORM(0,100,RANDOM()) < 45 THEN 'CRYPTO_GIFTCARD' ELSE {CATS} END"
               if d in DRIFT_DAYS else CATS)
        base_date = f"DATEADD(day,-{d},CURRENT_DATE())"
        sf.execute(f"""INSERT INTO {SRC}.ORDERS_RAW
        SELECT {offset} + SEQ8() AS ORDER_ID,
          DATEADD(second, UNIFORM(0,86399,RANDOM()), {base_date}::timestamp_ntz) AS EVENT_TS,
          {base_date} AS ORDER_DATE,
          {cust} AS CUSTOMER_ID,
          {cat} AS PRODUCT_CATEGORY,
          {amt} AS AMOUNT,
          ARRAY_CONSTRUCT('NEW','PAID','SHIPPED','CANCELLED')[UNIFORM(0,3,RANDOM())]::string AS STATUS,
          ARRAY_CONSTRUCT('NA','EU','APAC','LATAM')[UNIFORM(0,3,RANDOM())]::string AS REGION,
          DATEADD(hour,6,{base_date}::timestamp_ntz) AS LOADED_AT
        FROM TABLE(GENERATOR(ROWCOUNT => {cnt}))""")
        tags = []
        if d == SPIKE_DAY: tags.append("VOLUME SPIKE")
        if d == DROP_DAY: tags.append("VOLUME DROP")
        if np: tags.append(f"{np}% NULLs")
        if d in DRIFT_DAYS: tags.append("category drift")
        offset += cnt
        # duplicate ~3% of this day's rows to create a dedup signal
        if d == DUP_DAY:
            sf.execute(f"INSERT INTO {SRC}.ORDERS_RAW SELECT * FROM "
                       f"(SELECT * FROM {SRC}.ORDERS_RAW WHERE ORDER_DATE = {base_date}) SAMPLE (3)")
            tags.append("~3% duplicate rows")
        print(f"  day -{d:<2}: {cnt:>6,} rows  {'· '+', '.join(tags) if tags else ''}")

    # curate: dedup on ORDER_ID, drop null keys/amounts
    sf.execute(f"""CREATE OR REPLACE TABLE {CUR}.ORDERS_CURATED AS
        SELECT ORDER_ID, EVENT_TS, ORDER_DATE, CUSTOMER_ID, PRODUCT_CATEGORY, AMOUNT, STATUS, REGION, LOADED_AT
        FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY ORDER_ID ORDER BY EVENT_TS) rn
              FROM {SRC}.ORDERS_RAW WHERE CUSTOMER_ID IS NOT NULL AND AMOUNT IS NOT NULL)
        WHERE rn = 1""")

    # publish: one metrics row per day
    sf.execute(f"""CREATE OR REPLACE TABLE {PUB}.DAILY_ORDER_SUMMARY AS
        SELECT ORDER_DATE,
          COUNT(*) AS ORDERS_COUNT,
          COUNT(DISTINCT CUSTOMER_ID) AS DISTINCT_CUSTOMERS,
          ROUND(SUM(AMOUNT),2) AS TOTAL_REVENUE,
          ROUND(AVG(AMOUNT),2) AS AVG_ORDER_VALUE,
          COUNT(DISTINCT PRODUCT_CATEGORY) AS CATEGORY_COUNT,
          MAX(LOADED_AT) AS LAST_LOADED_AT
        FROM {CUR}.ORDERS_CURATED GROUP BY ORDER_DATE ORDER BY ORDER_DATE""")

    sc = int(sf.execute(f"SELECT COUNT(*) FROM {SRC}.ORDERS_RAW").rows[0][0])
    cc = int(sf.execute(f"SELECT COUNT(*) FROM {CUR}.ORDERS_CURATED").rows[0][0])
    pc = int(sf.execute(f"SELECT COUNT(*) FROM {PUB}.DAILY_ORDER_SUMMARY").rows[0][0])
    span = sf.execute(f"SELECT MIN(ORDER_DATE)||' .. '||MAX(ORDER_DATE) FROM {SRC}.ORDERS_RAW").rows[0][0]
    print(f"\nsource ORDERS_RAW         {sc:>8,} rows   span {span}")
    print(f"curate ORDERS_CURATED     {cc:>8,} rows   (deduped + null-key dropped)")
    print(f"publish DAILY_ORDER_SUMMARY {pc:>6,} rows   (1/day; missing day absent → completeness gap)")


if __name__ == "__main__":
    main()
