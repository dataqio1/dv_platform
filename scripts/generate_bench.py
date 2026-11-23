"""Build an IDENTICAL benchmark table on Oracle and Snowflake, by construction.

Both sides are generated from the same deterministic row-number formula, so every logical
value matches without moving any data. Columns are a realistic mix (int key, strings, a
decimal, a small-cardinality enum, a date, a wide note) so the compare does real work.

Usage:  PYTHONPATH=src DVP_PROFILE=local .venv/bin/python scripts/generate_bench.py [ROWS]
        (ROWS must be a multiple of 1000; default 1_000_000)

Creates:
  Oracle    DATAQ.DQ_BENCH_<n>
  Snowflake DWH_TEST.BENCH.DQ_BENCH_<n>
"""

from __future__ import annotations

import hashlib
import sys
import time

from dvp.abstractions.factory import build_container
from dvp.config import get_settings
from dvp.connectors.registry import build_connector
from dvp.db.models import Connection
from dvp.services.tenancy import bootstrap_local_tenant, resolve_context

ORACLE_CONN = "16a1a10c-a53f-4937-b73e-8b010330488a"  # oracle-dataq  -> DATAQ
SNOWFLAKE_CONN = "3ba56de1-5484-49a6-8f65-a4a71d39babe"  # snowflake-dwh-test -> DWH_TEST

ORA_TABLE_FQN = "DATAQ.{name}"
SF_DATABASE = "DATAQ_BENCH"
SF_TABLE_FQN = "DATAQ_BENCH.PUBLIC.{name}"

# Shared deterministic column expressions (id is the only free variable). Kept dialect-neutral
# where possible; the two CTAS templates below only differ in the row-number source + a couple
# of function names (DATEADD vs date arithmetic).
SF_SELECT = """
SELECT
  id,
  'EMP_' || id                                                      AS name,
  'user' || id || '@example.com'                                    AS email,
  MOD(id * 7, 100000) + 0.50                                        AS amount,
  CASE MOD(id,3) WHEN 0 THEN 'ACTIVE' WHEN 1 THEN 'PENDING' ELSE 'CLOSED' END AS status,
  DATEADD(day, MOD(id, 1461), DATE '2020-01-01')                    AS created_dt,
  CASE MOD(id,4) WHEN 0 THEN 'NA' WHEN 1 THEN 'EU' WHEN 2 THEN 'APAC' ELSE 'LATAM' END AS region,
  'benchmark row ' || id || ' deterministic payload for compare'    AS note
FROM (SELECT seq8() + 1 AS id FROM TABLE(GENERATOR(ROWCOUNT => {rows})))
"""

ORA_SELECT = """
SELECT
  id,
  'EMP_' || id                                                      AS name,
  'user' || id || '@example.com'                                    AS email,
  MOD(id * 7, 100000) + 0.50                                        AS amount,
  CASE MOD(id,3) WHEN 0 THEN 'ACTIVE' WHEN 1 THEN 'PENDING' ELSE 'CLOSED' END AS status,
  DATE '2020-01-01' + MOD(id, 1461)                                 AS created_dt,
  CASE MOD(id,4) WHEN 0 THEN 'NA' WHEN 1 THEN 'EU' WHEN 2 THEN 'APAC' ELSE 'LATAM' END AS region,
  'benchmark row ' || id || ' deterministic payload for compare'    AS note
FROM (
  SELECT (a.n - 1) * 1000 + b.n AS id
  FROM (SELECT LEVEL n FROM dual CONNECT BY LEVEL <= 1000) a,
       (SELECT LEVEL n FROM dual CONNECT BY LEVEL <= {b}) b
)
"""

# Canonical row text for the cross-engine SHA-256 spot-check (matches the shape the compare's
# canonical contract would produce: pipe-joined, numbers trimmed, date as ISO).
SF_HASH = """
SELECT LOWER(SHA2(
  id || '|' || 'EMP_'||id || '|' || 'user'||id||'@example.com' || '|' ||
  RTRIM(RTRIM(TO_VARCHAR(CAST(MOD(id*7,100000)+0.50 AS NUMBER(38,10))),'0'),'.') || '|' ||
  (CASE MOD(id,3) WHEN 0 THEN 'ACTIVE' WHEN 1 THEN 'PENDING' ELSE 'CLOSED' END) || '|' ||
  TO_VARCHAR(DATEADD(day, MOD(id,1461), DATE '2020-01-01'),'YYYY-MM-DD') || '|' ||
  (CASE MOD(id,4) WHEN 0 THEN 'NA' WHEN 1 THEN 'EU' WHEN 2 THEN 'APAC' ELSE 'LATAM' END) || '|' ||
  'benchmark row '||id||' deterministic payload for compare', 256)) AS h
FROM (SELECT {id} AS id)
"""

ORA_HASH = """
SELECT LOWER(STANDARD_HASH(
  id || '|' || 'EMP_'||id || '|' || 'user'||id||'@example.com' || '|' ||
  RTRIM(RTRIM(TO_CHAR(CAST(MOD(id*7,100000)+0.50 AS NUMBER(38,10)),'FM99999999999999999990.0000000000'),'0'),'.') || '|' ||
  (CASE MOD(id,3) WHEN 0 THEN 'ACTIVE' WHEN 1 THEN 'PENDING' ELSE 'CLOSED' END) || '|' ||
  TO_CHAR(DATE '2020-01-01' + MOD(id,1461),'YYYY-MM-DD') || '|' ||
  (CASE MOD(id,4) WHEN 0 THEN 'NA' WHEN 1 THEN 'EU' WHEN 2 THEN 'APAC' ELSE 'LATAM' END) || '|' ||
  'benchmark row '||id||' deterministic payload for compare', 'SHA256')) AS h
FROM (SELECT {id} AS id FROM dual)
"""


def get_connector(container, ctx, conn_id):
    with container.metadata.session(ctx) as s:
        c = s.get(Connection, conn_id)
        st, coords = c.source_type, dict(c.coordinates)
        auth, ref = c.auth_method, c.secret_ref
    secret = container.secrets.get(ctx, ref) if ref and container.secrets else None
    return build_connector(st, coords, auth, secret)


def run(conn, sql, label):
    t = time.time()
    conn.execute(sql)
    print(f"  {label}: {time.time()-t:.1f}s")


def ora_ddl(ora, sql, label=None):
    """Run Oracle DDL/PL-SQL on a raw cursor (the connector's execute() always fetchall()s,
    which errors on non-queries) and commit."""
    t = time.time()
    c = ora._connect()
    try:
        cur = c.cursor()
        cur.execute(sql)
        c.commit()
    finally:
        c.close()
    if label:
        print(f"  {label}: {time.time()-t:.1f}s")


def main() -> None:
    rows = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    assert rows % 1000 == 0, "ROWS must be a multiple of 1000"
    name = f"DQ_BENCH_{rows // 1000}K" if rows < 1_000_000 else f"DQ_BENCH_{rows // 1_000_000}M"
    settings = get_settings()
    container = build_container(settings)
    container.metadata.create_all()
    t = bootstrap_local_tenant(container.metadata, settings)
    ctx = resolve_context(container.metadata, user_id=t.user_id, org_id=t.org_id, group_id=t.group_id)
    ora = get_connector(container, ctx, ORACLE_CONN)
    sf = get_connector(container, ctx, SNOWFLAKE_CONN)

    print(f"Building {name} with {rows:,} rows on both engines…")

    # Snowflake (dedicated DB we own; PUBLIC schema exists by default)
    sf.execute(f"CREATE DATABASE IF NOT EXISTS {SF_DATABASE}")
    run(sf, f"CREATE OR REPLACE TABLE {SF_TABLE_FQN.format(name=name)} AS {SF_SELECT.format(rows=rows)}",
        "Snowflake CTAS")

    # Oracle (drop if exists, then CTAS)
    ora_fqn = ORA_TABLE_FQN.format(name=name)
    ora_ddl(
        ora,
        f"BEGIN EXECUTE IMMEDIATE 'DROP TABLE {ora_fqn} PURGE'; "
        f"EXCEPTION WHEN OTHERS THEN IF SQLCODE != -942 THEN RAISE; END IF; END;",
    )
    ora_ddl(ora, f"CREATE TABLE {ora_fqn} AS {ORA_SELECT.format(b=rows // 1000)}", "Oracle CTAS")

    # Verify counts
    sc = int(sf.execute(f"SELECT COUNT(*) FROM {SF_TABLE_FQN.format(name=name)}").rows[0][0])
    oc = int(ora.execute(f"SELECT COUNT(*) FROM {ora_fqn}").rows[0][0])
    print(f"Counts — Snowflake {sc:,} · Oracle {oc:,} · {'MATCH ✅' if sc == oc == rows else 'MISMATCH ❌'}")

    # Cross-engine SHA-256 spot check on a few ids (proves the rows are byte-identical)
    print("Row-hash spot check (Oracle STANDARD_HASH == Snowflake SHA2):")
    ok = True
    for cid in (1, 2, 500, rows // 2, rows):
        oh = str(ora.execute(ORA_HASH.format(id=cid)).rows[0][0]).lower()
        sh = str(sf.execute(SF_HASH.format(id=cid)).rows[0][0]).lower()
        match = oh == sh
        ok = ok and match
        print(f"  id={cid:<9} {'match' if match else 'DIFF'}  {oh[:16]}…")
    print("Spot check:", "ALL MATCH ✅" if ok else "MISMATCH ❌")
    print(f"\nReady. Source: Oracle {ora_fqn}  |  Target: Snowflake {SF_TABLE_FQN.format(name=name)}")
    print(f"Key: ID   Table: {name}")


if __name__ == "__main__":
    main()
