"""Copy a varied-shape set of TPC-H tables from Snowflake into the new Oracle (DATAQ schema).

Source : SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.{REGION,NATION,SUPPLIER,CUSTOMER,ORDERS}
Target : Oracle DATAQ.<TABLE>  (structure derived from the Snowflake column metadata)

Rows are streamed SF->Oracle in batches (fetchmany/executemany) so ORDERS (1.5M) stays bounded
in memory. Each table is dropped+recreated, loaded, then row counts are verified on both sides.

Usage: PYTHONPATH=src DVP_PROFILE=local .venv/bin/python scripts/copy_tpch_to_oracle.py
"""

from __future__ import annotations

import time

from dvp.abstractions.factory import build_container
from dvp.config import get_settings
from dvp.connectors.registry import build_connector
from dvp.db.models import Connection
from dvp.services.tenancy import bootstrap_local_tenant, resolve_context

ORACLE_CONN = "16a1a10c-a53f-4937-b73e-8b010330488a"      # oracle-dataq -> DATAQ @ new VM
SNOWFLAKE_CONN = "3ba56de1-5484-49a6-8f65-a4a71d39babe"   # snowflake-dwh-test

SRC_SCHEMA = "SNOWFLAKE_SAMPLE_DATA.TPCH_SF1"
INFO_SCHEMA = "SNOWFLAKE_SAMPLE_DATA.INFORMATION_SCHEMA.COLUMNS"
TARGET_SCHEMA = "DATAQ"
TABLES = ["REGION", "NATION", "SUPPLIER", "CUSTOMER", "ORDERS"]
BATCH = 50_000


def get_connector(container, ctx, conn_id):
    with container.metadata.session(ctx) as s:
        c = s.get(Connection, conn_id)
        st, coords = c.source_type, dict(c.coordinates)
        auth, ref = c.auth_method, c.secret_ref
    secret = container.secrets.get(ctx, ref) if ref and container.secrets else None
    return build_connector(st, coords, auth, secret)


def ora_type(data_type, prec, scale, clen):
    dt = data_type.upper()
    if dt == "NUMBER":
        if scale and int(scale) > 0:
            return f"NUMBER({int(prec)},{int(scale)})"
        p = int(prec) if prec else 38
        return f"NUMBER({min(p,38)})"
    if dt in ("TEXT", "VARCHAR", "STRING", "CHAR"):
        n = int(clen) if clen else 4000
        return f"VARCHAR2({min(n,4000)} CHAR)"
    if dt == "DATE":
        return "DATE"
    if dt.startswith("TIMESTAMP"):
        return "TIMESTAMP"
    if dt in ("FLOAT", "DOUBLE", "REAL"):
        return "BINARY_DOUBLE"
    return "VARCHAR2(4000 CHAR)"


def main():
    s = get_settings()
    c = build_container(s)
    c.metadata.create_all()
    t = bootstrap_local_tenant(c.metadata, s)
    ctx = resolve_context(c.metadata, user_id=t.user_id, org_id=t.org_id, group_id=t.group_id)
    sf = get_connector(c, ctx, SNOWFLAKE_CONN)
    ora = get_connector(c, ctx, ORACLE_CONN)

    sfc = sf._connect()
    orac = ora._connect()
    try:
        for tb in TABLES:
            t0 = time.time()
            # 1) column metadata from Snowflake
            meta = sf.execute(
                "SELECT column_name, data_type, numeric_precision, numeric_scale, character_maximum_length "
                f"FROM {INFO_SCHEMA} WHERE table_schema='TPCH_SF1' AND table_name='{tb}' ORDER BY ordinal_position"
            ).rows
            cols = [m[0] for m in meta]
            ddl_cols = ", ".join(f'{m[0]} {ora_type(m[1], m[2], m[3], m[4])}' for m in meta)
            fqn = f"{TARGET_SCHEMA}.{tb}"

            # 2) drop + create on Oracle
            oc = orac.cursor()
            oc.execute(
                f"BEGIN EXECUTE IMMEDIATE 'DROP TABLE {fqn} PURGE'; "
                f"EXCEPTION WHEN OTHERS THEN IF SQLCODE != -942 THEN RAISE; END IF; END;"
            )
            oc.execute(f"CREATE TABLE {fqn} ({ddl_cols})")
            orac.commit()

            # 3) stream rows SF -> Oracle
            binds = ", ".join(f":{i+1}" for i in range(len(cols)))
            ins = f"INSERT INTO {fqn} ({', '.join(cols)}) VALUES ({binds})"
            scur = sfc.cursor()
            scur.execute(f"SELECT {', '.join(cols)} FROM {SRC_SCHEMA}.{tb}")
            loaded = 0
            while True:
                rows = scur.fetchmany(BATCH)
                if not rows:
                    break
                oc.executemany(ins, [tuple(r) for r in rows])
                orac.commit()
                loaded += len(rows)
                if loaded % 500_000 == 0:
                    print(f"    {tb}: {loaded:,} rows…", flush=True)
            scur.close()

            # 4) verify
            sc = int(sf.execute(f"SELECT COUNT(*) FROM {SRC_SCHEMA}.{tb}").rows[0][0])
            ocnt = int(ora.execute(f"SELECT COUNT(*) FROM {fqn}").rows[0][0])
            ok = "MATCH ✅" if sc == ocnt else "MISMATCH ❌"
            print(f"  {tb:<9} cols={len(cols):<2} SF={sc:>9,} Oracle={ocnt:>9,} {ok}  ({time.time()-t0:.1f}s)", flush=True)
    finally:
        sfc.close()
        orac.close()
    print("\nDone. Oracle tables created in DATAQ:", ", ".join(TABLES))


if __name__ == "__main__":
    main()
