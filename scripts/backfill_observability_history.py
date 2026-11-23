"""Backfill 30 daily observation snapshots per monitor from the fixture's real per-day metrics,
so the observability pillars have history and the injected anomalies trip (spike/drop/null/missing)."""
from __future__ import annotations
from dvp.abstractions.factory import build_container
from dvp.config import get_settings
from dvp.connectors.registry import build_connector
from dvp.db.models import Connection, Monitor
from dvp.services.tenancy import bootstrap_local_tenant, resolve_context
from dvp.observability.observability_service import ProfilerService

SF="3ba56de1-5484-49a6-8f65-a4a71d39babe"

def main():
    s=get_settings(); c=build_container(s); c.metadata.create_all()
    t=bootstrap_local_tenant(c.metadata,s); ctx=resolve_context(c.metadata,user_id=t.user_id,org_id=t.org_id,group_id=t.group_id)
    with c.metadata.session(ctx) as se:
        x=se.get(Connection,SF); sf=build_connector(x.source_type,dict(x.coordinates),x.auth_method,c.secrets.get(ctx,x.secret_ref))
        mons={m.name:(m.id,m.scope) for m in se.query(Monitor).all() if m.name.startswith("Observability ·")}
    prof=ProfilerService(c.metadata, c.secrets)

    def fqn(scope): return f"{scope['database']}.{scope['schema']}.{scope['tables'][0]}"

    # (monitor-name substr, per-day SQL, has_null_cols)
    jobs=[
      ("source", "DATA_OBSERVABILITY.DATA_OBSERVABILITY_SOURCE.ORDERS_RAW", True),
      ("curate", "DATA_OBSERVABILITY.DATA_OBSERVABILITY_CURATE.ORDERS_CURATED", True),
      ("publish","DATA_OBSERVABILITY.DATA_OBSERVABILITY_PUBLISH.DAILY_ORDER_SUMMARY", False),
    ]
    for key, tbl, has_nulls in jobs:
        name=[n for n in mons if key in n][0]; mid,scope=mons[name]; ref=fqn(scope)
        if key=="publish":
            rows=sf.execute(f"SELECT TO_CHAR(ORDER_DATE,'YYYY-MM-DD'), ORDERS_COUNT, TO_CHAR(LAST_LOADED_AT) "
                            f"FROM {tbl} ORDER BY ORDER_DATE").rows
            days=[(r[0], int(r[1]), 0.0, 0.0, r[2]) for r in rows]
        else:
            rows=sf.execute(f"""SELECT TO_CHAR(ORDER_DATE,'YYYY-MM-DD'), COUNT(*),
                SUM(IFF(AMOUNT IS NULL,1,0))/COUNT(*), SUM(IFF(CUSTOMER_ID IS NULL,1,0))/COUNT(*),
                TO_CHAR(MAX(EVENT_TS)) FROM {tbl} GROUP BY ORDER_DATE ORDER BY ORDER_DATE""").rows
            days=[(r[0], int(r[1]), float(r[2]), float(r[3]), r[4]) for r in rows]
        n=0
        for d, cnt, amt_nr, cust_nr, max_ts in days:   # chronological → baseline builds before anomalies
            obs={"ts": f"{d}T06:30:00", "row_count": cnt, "rows_analyzed": cnt,
                 "last_load_ts": max_ts or f"{d}T06:00:00"}
            if has_nulls:
                obs["columns"]=[{"name":"AMOUNT","null_rate":amt_nr,"distinct_ratio":0.9},
                                {"name":"CUSTOMER_ID","null_rate":cust_nr,"distinct_ratio":0.95}]
            prof.record_snapshot(ctx, mid, ref, obs); n+=1
        print(f"  {name[:40]:<40} backfilled {n} daily snapshots ({ref.split('.')[-1]})")
    print("done")

if __name__=="__main__":
    main()
