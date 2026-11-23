"""PoC: row-hash merge-join as the missing tier between fingerprint(bucket) and raw streaming.

Thesis: on SCATTERED diffs the engine bails from bucket-checksum to pulling *raw* rows (30 cols,
both sides). But both engines produce an identical canonical row hash (STANDARD_HASH == SHA2 — we
proved it). So instead of moving raw rows, move only pushdown (key, row_hash) pairs and join on
them; fetch full rows only for mismatched keys. This PoC measures that against the raw-streaming
baseline on the same data.

Runs a small-range SANITY check first (base→0 diffs, D01→1000 diffs over ids 1..100000), then a
full 10M row-hash compare of Oracle DATAQ.DQ_BENCH_10M vs a Snowflake variant.

Usage: PYTHONPATH=src DVP_PROFILE=local .venv/bin/python scripts/rowhash_poc.py [VARIANT]
       VARIANT in {_D01,_D05,_D10,_C10,""}; default _D01
"""
from __future__ import annotations
import sys, time
from dvp.abstractions.factory import build_container
from dvp.config import get_settings
from dvp.connectors.registry import build_connector
from dvp.db.models import Connection
from dvp.services.tenancy import bootstrap_local_tenant, resolve_context

ORACLE_CONN="16a1a10c-a53f-4937-b73e-8b010330488a"
SNOWFLAKE_CONN="3ba56de1-5484-49a6-8f65-a4a71d39babe"
ORA_TABLE="DATAQ.DQ_BENCH_10M"
SF_DB="DATAQ_BENCH.PUBLIC.DQ_BENCH_10M"
TOTAL=10_000_000
W=500_000

INT={"ID","CUST_ID","ORDER_NO","QTY","SCORE","RATING","MANAGER_ID","SALARY","AGE","LOGIN_COUNT","FLAG_ACTIVE"}
DEC={"AMOUNT","BALANCE","PRICE","DISCOUNT","BONUS"}
DATE={"CREATED_DT","UPDATED_DT"}
ORDER=["ID","CUST_ID","ORDER_NO","QTY","SCORE","RATING","MANAGER_ID","SALARY","AGE","LOGIN_COUNT",
       "AMOUNT","BALANCE","PRICE","DISCOUNT","BONUS","NAME","EMAIL","FIRST_NAME","LAST_NAME","CITY",
       "DEPT","CODE","NOTE","STATUS","REGION","TIER","COUNTRY","FLAG_ACTIVE","CREATED_DT","UPDATED_DT"]

def piece(col, eng):
    if col in DEC:
        return (f"TO_CHAR(CAST({col} AS NUMBER(38,2)),'FM999999999999990.00')" if eng=="ora"
                else f"TO_VARCHAR(CAST({col} AS NUMBER(38,2)))")
    if col in DATE:
        return f"TO_CHAR({col},'YYYY-MM-DD')" if eng=="ora" else f"TO_VARCHAR({col},'YYYY-MM-DD')"
    return f"({col})"
def payload(eng): return " || '|' || ".join(piece(c,eng) for c in ORDER)
ORA_H=f"LOWER(STANDARD_HASH({payload('ora')},'SHA256'))"
SF_H =f"LOWER(SHA2({payload('sf')},256))"

def conn(container,ctx,cid):
    with container.metadata.session(ctx) as s:
        c=s.get(Connection,cid); st,co,am,ref=c.source_type,dict(c.coordinates),c.auth_method,c.secret_ref
    sec=container.secrets.get(ctx,ref) if ref and container.secrets else None
    return build_connector(st,co,am,sec)

def hashes(connector, table, hexpr, lo, hi):
    sql=f"SELECT ID, {hexpr} FROM {table} WHERE ID > {lo} AND ID <= {hi}"
    return {int(r[0]): r[1] for r in connector.execute(sql).rows}

def compare(ora, sf, sf_table, lo_total, hi_total, window):
    matched=mism=so=do=0; bytes_moved=0
    lo=lo_total
    while lo < hi_total:
        hi=min(lo+window, hi_total)
        oh=hashes(ora, ORA_TABLE, ORA_H, lo, hi)
        sh=hashes(sf, sf_table, SF_H, lo, hi)
        bytes_moved += (len(oh)+len(sh))*72  # ~ (8-byte key + 64-char hash)
        for k,hv in oh.items():
            if k in sh: matched += (sh[k]==hv); mism += (sh[k]!=hv)
            else: so+=1
        for k in sh:
            if k not in oh: do+=1
        lo=hi
    return dict(matched=matched, mism=mism, src_only=so, dst_only=do,
                diffs=mism+so+do, approx_mb=round(bytes_moved/1e6,1))

def main():
    variant = sys.argv[1] if len(sys.argv)>1 else "_D01"
    s=get_settings(); c=build_container(s); c.metadata.create_all()
    t=bootstrap_local_tenant(c.metadata,s)
    ctx=resolve_context(c.metadata,user_id=t.user_id,org_id=t.org_id,group_id=t.group_id)
    ora=conn(c,ctx,ORACLE_CONN); sf=conn(c,ctx,SNOWFLAKE_CONN)

    print("== SANITY (ids 1..100000) ==", flush=True)
    base=compare(ora,sf,SF_DB,0,100_000,100_000)
    d01 =compare(ora,sf,SF_DB+"_D01",0,100_000,100_000)
    print(f"  base vs base  -> diffs={base['diffs']} (expect 0)   {'OK' if base['diffs']==0 else 'BAD'}", flush=True)
    print(f"  base vs _D01  -> diffs={d01['diffs']} (expect 1000) {'OK' if d01['diffs']==1000 else 'BAD'}", flush=True)
    if base['diffs']!=0 or d01['diffs']!=1000:
        print("  sanity failed — canonical hash mismatch; aborting full run."); return

    print(f"\n== FULL row-hash compare: Oracle DQ_BENCH_10M  vs  Snowflake DQ_BENCH_10M{variant} ==", flush=True)
    t0=time.time(); r=compare(ora,sf,SF_DB+variant,0,TOTAL,W); el=time.time()-t0
    print(f"  diffs={r['diffs']:,}  (matched={r['matched']:,} mism={r['mism']:,} src_only={r['src_only']} dst_only={r['dst_only']})", flush=True)
    print(f"  data moved ≈ {r['approx_mb']} MB (2 cols, both sides)   time={el:.0f}s", flush=True)

if __name__=="__main__":
    main()
