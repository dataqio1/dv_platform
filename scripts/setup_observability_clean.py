"""Clean setup: recreate the 3 observability monitors and backfill a tidy 30-day per-day history
(full column list → no false schema diff; no stray full-table run-snapshot)."""
from __future__ import annotations
import json, urllib.request, urllib.error
from dvp.abstractions.factory import build_container
from dvp.config import get_settings
from dvp.connectors.registry import build_connector
from dvp.db.models import Connection, Monitor
from dvp.services.tenancy import bootstrap_local_tenant, resolve_context
from dvp.observability.observability_service import ProfilerService

API="http://127.0.0.1:8000/api/v1"; SF="3ba56de1-5484-49a6-8f65-a4a71d39babe"; PROJECT="3cca2d8d-3f88-445c-9102-e73a6a9a4113"
DB="DATA_OBSERVABILITY"
def api(method, path, body=None):
    data=json.dumps(body).encode() if body is not None else None
    r=urllib.request.Request(API+path, data=data, method=method, headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp: return json.loads(resp.read())
    except urllib.error.HTTPError as e: return {"__error__":e.code,"body":e.read().decode()[:200]}

SRC_COLS=["ORDER_ID","EVENT_TS","ORDER_DATE","CUSTOMER_ID","PRODUCT_CATEGORY","AMOUNT","STATUS","REGION","LOADED_AT"]
PUB_COLS=["ORDER_DATE","ORDERS_COUNT","DISTINCT_CUSTOMERS","TOTAL_REVENUE","AVG_ORDER_VALUE","CATEGORY_COUNT","LAST_LOADED_AT"]
MONS=[
 dict(name="Observability · source (ORDERS_RAW)", schema="DATA_OBSERVABILITY_SOURCE", table="ORDERS_RAW",
      wm="EVENT_TS", wt="timestamp", layer="bronze", cols=SRC_COLS, nullcols=True),
 dict(name="Observability · curate (ORDERS_CURATED)", schema="DATA_OBSERVABILITY_CURATE", table="ORDERS_CURATED",
      wm="EVENT_TS", wt="timestamp", layer="silver", cols=SRC_COLS, nullcols=True),
 dict(name="Observability · publish (DAILY_ORDER_SUMMARY)", schema="DATA_OBSERVABILITY_PUBLISH", table="DAILY_ORDER_SUMMARY",
      wm="ORDER_DATE", wt="date", layer="gold", cols=PUB_COLS, nullcols=False),
]

def main():
    s=get_settings(); c=build_container(s); c.metadata.create_all()
    t=bootstrap_local_tenant(c.metadata,s); ctx=resolve_context(c.metadata,user_id=t.user_id,org_id=t.org_id,group_id=t.group_id)
    with c.metadata.session(ctx) as se:
        x=se.get(Connection,SF); sf=build_connector(x.source_type,dict(x.coordinates),x.auth_method,c.secrets.get(ctx,x.secret_ref))
    # delete any existing observability monitors (clears their observations via cascade)
    for m in api("GET","/monitors")["data"]:
        if m["name"].startswith("Observability ·"): api("DELETE", f"/monitors/{m['id']}")
    prof=ProfilerService(c.metadata, c.secrets)

    for m in MONS:
        created=api("POST","/monitors",{"connection_id":SF,"name":m["name"],
            "scope":{"database":DB,"schema":m["schema"],"tables":[m["table"]]},
            "watermark_column":m["wm"],"watermark_type":m["wt"],"load_type":"append","lookback_days":30,
            "project_id":PROJECT,"layer":m["layer"],"source_system":"orders_observability"})
        mid=created["data"]["id"]; ref=f"{DB}.{m['schema']}.{m['table']}"
        if m["table"]=="DAILY_ORDER_SUMMARY":
            rows=sf.execute(f"SELECT TO_CHAR(ORDER_DATE,'YYYY-MM-DD'),ORDERS_COUNT,TO_CHAR(LAST_LOADED_AT) FROM {ref} ORDER BY ORDER_DATE").rows
            days=[(r[0],int(r[1]),0.0,0.0,r[2]) for r in rows]
        else:
            rows=sf.execute(f"""SELECT TO_CHAR(ORDER_DATE,'YYYY-MM-DD'),COUNT(*),
              SUM(IFF(AMOUNT IS NULL,1,0))/COUNT(*),SUM(IFF(CUSTOMER_ID IS NULL,1,0))/COUNT(*),TO_CHAR(MAX(EVENT_TS))
              FROM {ref} GROUP BY ORDER_DATE ORDER BY ORDER_DATE""").rows
            days=[(r[0],int(r[1]),float(r[2]),float(r[3]),r[4]) for r in rows]
        for d,cnt,amt_nr,cust_nr,max_ts in days:
            cols=[{"name":col,
                   "null_rate":(amt_nr if col=="AMOUNT" else cust_nr if col=="CUSTOMER_ID" else 0.0),
                   "distinct_ratio":0.9} for col in m["cols"]]
            prof.record_snapshot(ctx, mid, ref,
                {"ts":f"{d}T06:30:00","row_count":cnt,"rows_analyzed":cnt,"last_load_ts":max_ts or f"{d}T06:00:00","columns":cols})
        print(f"  {m['name'][:40]:<40} {len(days)} days, {len(m['cols'])} cols  [{mid[:8]}]")
    print("done")

if __name__=="__main__":
    main()
