"""Build a 30-column benchmark table on Oracle (clean 'truth') + Snowflake (identical base plus
small-variation copies), by construction from a deterministic row-number formula.

Every logical value is a pure function of `id`, computed with engine-equivalent expressions, so
the two base tables are byte-identical (verified by a cross-engine SHA-256 spot check:
Oracle STANDARD_HASH == Snowflake SHA2). Then we derive Snowflake diff variants by perturbing a
few columns on a subset of rows — these are the "variations" used for performance metrics.

Creates:
  Oracle     DATAQ.DQ_BENCH_<n>                         (clean source of truth)
  Snowflake  DATAQ_BENCH.PUBLIC.DQ_BENCH_<n>            (identical base)
  Snowflake  DATAQ_BENCH.PUBLIC.DQ_BENCH_<n>_D01/_D05/_D10/_C10   (1/5/10% scattered, 10% clustered)

Usage:  PYTHONPATH=src DVP_PROFILE=local .venv/bin/python scripts/generate_bench_wide.py [ROWS]
        (ROWS multiple of 1000; default 10_000_000)
"""

from __future__ import annotations

import sys
import time

from dvp.abstractions.factory import build_container
from dvp.config import get_settings
from dvp.connectors.registry import build_connector
from dvp.db.models import Connection
from dvp.services.tenancy import bootstrap_local_tenant, resolve_context

ORACLE_CONN = "16a1a10c-a53f-4937-b73e-8b010330488a"      # oracle-dataq -> DATAQ
SNOWFLAKE_CONN = "3ba56de1-5484-49a6-8f65-a4a71d39babe"   # snowflake-dwh-test
SF_DATABASE = "DATAQ_BENCH"
SF_FQN = "DATAQ_BENCH.PUBLIC.{name}"
ORA_FQN = "DATAQ.{name}"

# ---- 30-column schema. Each entry: (name, select_expr, hash_expr) per engine. ---------------
# Where Oracle and Snowflake differ only in a function name, we note both; otherwise the
# expression string is shared. `h` is the canonical text form used for the row hash.
_DEC = "0.00"  # fixed 2-decimal canonical form


def dec_h_ora(expr):
    return f"TO_CHAR(CAST({expr} AS NUMBER(38,2)),'FM999999999999990.00')"


def dec_h_sf(expr):
    return f"TO_VARCHAR(CAST({expr} AS NUMBER(38,2)))"


# (name, ora_select, sf_select, ora_hash, sf_hash)
def build_cols():
    cols = []

    def intcol(name, expr):
        cols.append((name, expr, expr, expr, expr))

    def deccol(name, expr):
        cols.append((name, expr, expr, dec_h_ora(expr), dec_h_sf(expr)))

    def strcol(name, expr):
        cols.append((name, expr, expr, expr, expr))

    # 10 integer columns
    intcol("ID", "id")
    intcol("CUST_ID", "MOD(id,1000000)")
    intcol("ORDER_NO", "id*2")
    intcol("QTY", "MOD(id,500)")
    intcol("SCORE", "MOD(id*31,1000)")
    intcol("RATING", "MOD(id,5)+1")
    intcol("MANAGER_ID", "MOD(id,10000)")
    intcol("SALARY", "30000 + MOD(id*17,70000)")
    intcol("AGE", "MOD(id,60)+18")
    intcol("LOGIN_COUNT", "MOD(id*7,10000)")
    # 5 decimal(scale 2) columns
    deccol("AMOUNT", "MOD(id*7,100000) + 0.50")
    deccol("BALANCE", "MOD(id*13,1000000)/100")
    deccol("PRICE", "MOD(id*101,50000) + 0.99")
    deccol("DISCOUNT", "MOD(id*3,100)/100")
    deccol("BONUS", "MOD(id*19,20000) + 0.25")
    # 8 string columns
    strcol("NAME", "'EMP_' || id")
    strcol("EMAIL", "'user' || id || '@example.com'")
    strcol("FIRST_NAME", "'First' || MOD(id,1000)")
    strcol("LAST_NAME", "'Last' || MOD(id,5000)")
    strcol("CITY", "'City_' || MOD(id,300)")
    strcol("DEPT", "'DEPT_' || MOD(id,20)")
    strcol("CODE", "'CODE' || MOD(id*7,999999)")
    strcol("NOTE", "'benchmark row ' || id || ' deterministic payload for compare'")
    # 4 enum columns
    strcol("STATUS", "CASE MOD(id,3) WHEN 0 THEN 'ACTIVE' WHEN 1 THEN 'PENDING' ELSE 'CLOSED' END")
    strcol("REGION", "CASE MOD(id,4) WHEN 0 THEN 'NA' WHEN 1 THEN 'EU' WHEN 2 THEN 'APAC' ELSE 'LATAM' END")
    strcol("TIER", "CASE MOD(id,5) WHEN 0 THEN 'BRONZE' WHEN 1 THEN 'SILVER' WHEN 2 THEN 'GOLD' WHEN 3 THEN 'PLATINUM' ELSE 'DIAMOND' END")
    strcol("COUNTRY", "CASE MOD(id,6) WHEN 0 THEN 'US' WHEN 1 THEN 'UK' WHEN 2 THEN 'DE' WHEN 3 THEN 'IN' WHEN 4 THEN 'JP' ELSE 'BR' END")
    # 1 flag int
    intcol("FLAG_ACTIVE", "MOD(id,2)")
    # 2 date columns (engine-specific date arithmetic)
    cols.append(("CREATED_DT",
                 "DATE '2020-01-01' + MOD(id,1461)",
                 "DATEADD(day, MOD(id,1461), DATE '2020-01-01')",
                 "TO_CHAR(DATE '2020-01-01' + MOD(id,1461),'YYYY-MM-DD')",
                 "TO_VARCHAR(DATEADD(day, MOD(id,1461), DATE '2020-01-01'),'YYYY-MM-DD')"))
    cols.append(("UPDATED_DT",
                 "DATE '2021-01-01' + MOD(id,1000)",
                 "DATEADD(day, MOD(id,1000), DATE '2021-01-01')",
                 "TO_CHAR(DATE '2021-01-01' + MOD(id,1000),'YYYY-MM-DD')",
                 "TO_VARCHAR(DATEADD(day, MOD(id,1000), DATE '2021-01-01'),'YYYY-MM-DD')"))
    assert len(cols) == 30, len(cols)
    return cols


COLS = build_cols()


def select_body(engine):
    idx = 1 if engine == "ora" else 2
    return ",\n  ".join(f"{c[idx]} AS {c[0]}" for c in COLS)


def hash_payload(engine):
    # Wrap each field in parentheses: in Oracle `||` binds tighter than binary +/-, so an
    # unparenthesized int expr like MOD(id,5)+1 would mis-associate with the concatenation.
    idx = 3 if engine == "ora" else 4
    return " || '|' || ".join(f"({c[idx]})" for c in COLS)


def ora_select(rows):
    outer = rows // 1000  # id = (a-1)*1000 + b ; a in 1..outer, b in 1..1000  -> unique 1..rows
    return f"""
SELECT
  {select_body('ora')}
FROM (
  SELECT (a.n - 1) * 1000 + b.n AS id
  FROM (SELECT LEVEL n FROM dual CONNECT BY LEVEL <= {outer}) a,
       (SELECT LEVEL n FROM dual CONNECT BY LEVEL <= 1000) b
)
"""


def sf_select(rows):
    return f"""
SELECT
  {select_body('sf')}
FROM (SELECT seq8() + 1 AS id FROM TABLE(GENERATOR(ROWCOUNT => {rows})))
"""


def ora_hash_sql(cid):
    return f"SELECT LOWER(STANDARD_HASH({hash_payload('ora')}, 'SHA256')) AS h FROM (SELECT {cid} AS id FROM dual)"


def sf_hash_sql(cid):
    return f"SELECT LOWER(SHA2({hash_payload('sf')}, 256)) AS h FROM (SELECT {cid} AS id)"


# Snowflake diff variants: perturb AMOUNT, STATUS, NOTE on the matched rows so the row differs.
VARIANTS = [
    ("D01", "MOD(id,100)=0", "1% scattered"),
    ("D05", "MOD(id,20)=0", "5% scattered"),
    ("D10", "MOD(id,10)=0", "10% scattered"),
    ("C10", "id <= {clustered}", "10% clustered block"),
]


def get_connector(container, ctx, conn_id):
    with container.metadata.session(ctx) as s:
        c = s.get(Connection, conn_id)
        st, coords = c.source_type, dict(c.coordinates)
        auth, ref = c.auth_method, c.secret_ref
    secret = container.secrets.get(ctx, ref) if ref and container.secrets else None
    return build_connector(st, coords, auth, secret)


def ora_ddl(ora, sql, label=None):
    t = time.time()
    c = ora._connect()
    try:
        cur = c.cursor()
        cur.execute(sql)
        c.commit()
    finally:
        c.close()
    if label:
        print(f"  {label}: {time.time()-t:.1f}s", flush=True)


def run_sf(sf, sql, label):
    t = time.time()
    sf.execute(sql)
    print(f"  {label}: {time.time()-t:.1f}s", flush=True)


def main():
    rows = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000_000
    assert rows % 1000 == 0, "ROWS must be a multiple of 1000"
    name = f"DQ_BENCH_{rows // 1_000_000}M" if rows >= 1_000_000 else f"DQ_BENCH_{rows // 1000}K"
    clustered = rows // 10

    s = get_settings()
    container = build_container(s)
    container.metadata.create_all()
    t = bootstrap_local_tenant(container.metadata, s)
    ctx = resolve_context(container.metadata, user_id=t.user_id, org_id=t.org_id, group_id=t.group_id)
    ora = get_connector(container, ctx, ORACLE_CONN)
    sf = get_connector(container, ctx, SNOWFLAKE_CONN)

    print(f"Building {name}: {rows:,} rows x {len(COLS)} cols on both engines…", flush=True)

    # Snowflake base
    sf.execute(f"CREATE DATABASE IF NOT EXISTS {SF_DATABASE}")
    sf_base = SF_FQN.format(name=name)
    run_sf(sf, f"CREATE OR REPLACE TABLE {sf_base} AS {sf_select(rows)}", "Snowflake base CTAS")

    # Oracle base (clean truth)
    ofqn = ORA_FQN.format(name=name)
    ora_ddl(ora, f"BEGIN EXECUTE IMMEDIATE 'DROP TABLE {ofqn} PURGE'; "
                 f"EXCEPTION WHEN OTHERS THEN IF SQLCODE != -942 THEN RAISE; END IF; END;")
    ora_ddl(ora, f"CREATE TABLE {ofqn} AS {ora_select(rows)}", "Oracle base CTAS")

    # Counts
    sc = int(sf.execute(f"SELECT COUNT(*) FROM {sf_base}").rows[0][0])
    oc = int(ora.execute(f"SELECT COUNT(*) FROM {ofqn}").rows[0][0])
    print(f"Counts — Snowflake {sc:,} · Oracle {oc:,} · "
          f"{'MATCH ✅' if sc == oc == rows else 'MISMATCH ❌'}", flush=True)

    # Cross-engine SHA-256 spot check (proves rows are byte-identical by construction)
    print("Row-hash spot check (Oracle STANDARD_HASH == Snowflake SHA2):", flush=True)
    ok = True
    for cid in (1, 2, 500, rows // 2, rows):
        oh = str(ora.execute(ora_hash_sql(cid)).rows[0][0]).lower()
        sh = str(sf.execute(sf_hash_sql(cid)).rows[0][0]).lower()
        m = oh == sh
        ok = ok and m
        print(f"  id={cid:<9} {'match' if m else 'DIFF'}  {oh[:16]}…", flush=True)
    print("Spot check:", "ALL MATCH ✅" if ok else "MISMATCH ❌", flush=True)

    # Snowflake diff variants
    print("Building Snowflake diff variants (AMOUNT/STATUS/NOTE perturbed on matched rows)…", flush=True)
    others = [c[0] for c in COLS if c[0] not in ("AMOUNT", "STATUS", "NOTE")]
    for suf, pred, desc in VARIANTS:
        pred = pred.format(clustered=clustered)
        vfqn = SF_FQN.format(name=f"{name}_{suf}")
        perturbed = (
            "CASE WHEN {p} THEN AMOUNT + 1.00 ELSE AMOUNT END AS AMOUNT, "
            "CASE WHEN {p} THEN 'CHANGED' ELSE STATUS END AS STATUS, "
            "CASE WHEN {p} THEN NOTE || ' [MODIFIED]' ELSE NOTE END AS NOTE"
        ).format(p=pred)
        sel = ", ".join(others) + ", " + perturbed
        run_sf(sf, f"CREATE OR REPLACE TABLE {vfqn} AS SELECT {sel} FROM {sf_base}", f"{suf} ({desc})")
        nd = int(sf.execute(f"SELECT COUNT(*) FROM {vfqn} WHERE STATUS='CHANGED'").rows[0][0])
        print(f"    {suf}: {nd:,} rows differ ({100.0*nd/rows:.1f}%)", flush=True)

    print(f"\nReady.\n  Oracle source (clean): {ofqn}\n  Snowflake base:        {sf_base}")
    print(f"  Snowflake variants:    {name}_D01, _D05, _D10, _C10")
    print("  Key: ID   (30 columns)")


if __name__ == "__main__":
    main()
