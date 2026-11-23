"""Fix freshness (and completeness) in-place on the existing observability monitors (preserve IDs).

Clears their observations and re-backfills with correct freshness semantics:
  - observation ts = day 23:00 (a check AFTER the ~06:00 load)
  - last_load_ts   = carried-forward actual load time (missing day keeps the prior day's load → stale)
  - missing day included with row_count 0 → volume-drop + freshness staleness spike
"""
from __future__ import annotations
import json
from dvp.abstractions.factory import build_container
from dvp.config import get_settings
from dvp.connectors.registry import build_connector
from dvp.db.models import Connection, Monitor, MetricDefinition, MetricObservation
from dvp.services.tenancy import bootstrap_local_tenant, resolve_context
from dvp.observability.observability_service import ProfilerService

SF="3ba56de1-5484-49a6-8f65-a4a71d39babe"
SRC_COLS=["ORDER_ID","EVENT_TS","ORDER_DATE","CUSTOMER_ID","PRODUCT_CATEGORY","AMOUNT","STATUS","REGION","LOADED_AT"]
PUB_COLS=["ORDER_DATE","ORDERS_COUNT","DISTINCT_CUSTOMERS","TOTAL_REVENUE","AVG_ORDER_VALUE","CATEGORY_COUNT","LAST_LOADED_AT"]

def main():
    s=get_settings(); c=build_container(s); c.metadata.create_all()
    t=bootstrap_local_tenant(c.metadata,s); ctx=resolve_context(c.metadata,user_id=t.user_id,org_id=t.org_id,group_id=t.group_id)
    with c.metadata.session(ctx) as se:
        x=se.get(Connection,SF); sf=build_connector(x.source_type,dict(x.coordinates),x.auth_method,c.secrets.get(ctx,x.secret_ref))
        mons={m.name:m.id for m in se.query(Monitor).all() if m.name.startswith("Observability ·")}
    prof=ProfilerService(c.metadata, c.secrets)

    # Global purge: observability pillars write to the `Metric` table keyed by table_ref. Delete all
    # DATA_OBSERVABILITY pillar points — INCLUDING orphans from previously-deleted monitors (pillar
    # history is table_ref-scoped, so orphans pollute the series and inflate the band).
    from dvp.db.models import Metric
    with c.metadata.session(ctx) as se:
        n=se.query(Metric).filter(Metric.table_ref.like("DATA_OBSERVABILITY%")).delete(synchronize_session=False)
        se.commit()
    print(f"  purged {n} observability pillar points")

    tables={
      "source": ("DATA_OBSERVABILITY.DATA_OBSERVABILITY_SOURCE.ORDERS_RAW", SRC_COLS, True, "LOADED_AT"),
      "curate": ("DATA_OBSERVABILITY.DATA_OBSERVABILITY_CURATE.ORDERS_CURATED", SRC_COLS, True, "LOADED_AT"),
      "publish":("DATA_OBSERVABILITY.DATA_OBSERVABILITY_PUBLISH.DAILY_ORDER_SUMMARY", PUB_COLS, False, "LAST_LOADED_AT"),
    }
    for key,(tbl,cols,has_nulls,loadcol) in tables.items():
        name=[n for n in mons if key in n][0]; mid=mons[name]
        # 30-day calendar LEFT JOIN so the missing day is present with 0 rows / NULL load
        if key=="publish":
            q=f"""WITH cal AS (SELECT DATEADD(day,-seq4(),CURRENT_DATE()) d FROM TABLE(GENERATOR(ROWCOUNT=>30))),
              agg AS (SELECT ORDER_DATE d, ORDERS_COUNT cnt, LAST_LOADED_AT lt FROM {tbl})
              SELECT TO_CHAR(cal.d,'YYYY-MM-DD'), COALESCE(agg.cnt,0), 0, 0, TO_CHAR(agg.lt)
              FROM cal LEFT JOIN agg ON cal.d=agg.d ORDER BY cal.d"""
        else:
            q=f"""WITH cal AS (SELECT DATEADD(day,-seq4(),CURRENT_DATE()) d FROM TABLE(GENERATOR(ROWCOUNT=>30))),
              agg AS (SELECT ORDER_DATE d, COUNT(*) cnt, SUM(IFF(AMOUNT IS NULL,1,0))/COUNT(*) a,
                      SUM(IFF(CUSTOMER_ID IS NULL,1,0))/COUNT(*) cu, MAX({loadcol}) lt FROM {tbl} GROUP BY ORDER_DATE)
              SELECT TO_CHAR(cal.d,'YYYY-MM-DD'), COALESCE(agg.cnt,0), COALESCE(agg.a,0), COALESCE(agg.cu,0), TO_CHAR(agg.lt)
              FROM cal LEFT JOIN agg ON cal.d=agg.d ORDER BY cal.d"""
        rows=sf.execute(q).rows
        last_load=None; miss=0
        for d,cnt,a,cu,lt in rows:
            if lt: last_load=lt
            if not lt: miss+=1
            cols_obs=[{"name":col,"null_rate":(float(a) if col=="AMOUNT" else float(cu) if col=="CUSTOMER_ID" else 0.0),
                       "distinct_ratio":0.9} for col in cols] if has_nulls else [{"name":col,"null_rate":0.0} for col in cols]
            prof.record_snapshot(ctx, mid, tbl, {
                "ts": f"{d}T23:00:00", "row_count": int(cnt), "rows_analyzed": int(cnt),
                "last_load_ts": last_load or f"{d}T06:00:00", "columns": cols_obs})
        print(f"  {name[:40]:<40} {len(rows)} days (missing={miss}) re-backfilled  [{mid[:8]}]")
    print("done")

if __name__=="__main__":
    main()
