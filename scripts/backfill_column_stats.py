"""Re-backfill per-column stats (real null-rate AND distinct-ratio) + freshness/volume, so the
column attention markers work — including cardinality drift (PRODUCT_CATEGORY gains a 6th value)."""
from __future__ import annotations
from dvp.abstractions.factory import build_container
from dvp.config import get_settings
from dvp.connectors.registry import build_connector
from dvp.db.models import Connection, Monitor, Metric
from dvp.services.tenancy import bootstrap_local_tenant, resolve_context
from dvp.observability.observability_service import ProfilerService

SF="3ba56de1-5484-49a6-8f65-a4a71d39babe"
SRC_COLS=["ORDER_ID","EVENT_TS","ORDER_DATE","CUSTOMER_ID","PRODUCT_CATEGORY","AMOUNT","STATUS","REGION","LOADED_AT"]

def main():
    s=get_settings(); c=build_container(s); c.metadata.create_all()
    t=bootstrap_local_tenant(c.metadata,s); ctx=resolve_context(c.metadata,user_id=t.user_id,org_id=t.org_id,group_id=t.group_id)
    with c.metadata.session(ctx) as se:
        x=se.get(Connection,SF); sf=build_connector(x.source_type,dict(x.coordinates),x.auth_method,c.secrets.get(ctx,x.secret_ref))
        mons={m.name:m.id for m in se.query(Monitor).all() if m.name.startswith("Observability ·")}
    prof=ProfilerService(c.metadata, c.secrets)
    tables={
      "source": "DATA_OBSERVABILITY.DATA_OBSERVABILITY_SOURCE.ORDERS_RAW",
      "curate": "DATA_OBSERVABILITY.DATA_OBSERVABILITY_CURATE.ORDERS_CURATED",
    }
    # purge only the two tables we rebuild here (leave publish's history intact)
    with c.metadata.session(ctx) as se:
        n=se.query(Metric).filter(Metric.table_ref.in_(list(tables.values()))).delete(synchronize_session=False); se.commit()
    print(f"  purged {n} pillar points (source+curate)")
    for key,tbl in tables.items():
        name=[nm for nm in mons if key in nm][0]; mid=mons[name]
        aggcols=", ".join(
            f'SUM(IFF({col} IS NULL,1,0))/COUNT(*) AS "{col}__nr", '
            f'COUNT(DISTINCT {col})/NULLIF(COUNT(*),0) AS "{col}__dr"' for col in SRC_COLS)
        selcols=", ".join(f'COALESCE(agg."{col}__nr",0), agg."{col}__dr"' for col in SRC_COLS)
        q=f"""WITH cal AS (SELECT DATEADD(day,-seq4(),CURRENT_DATE()) d FROM TABLE(GENERATOR(ROWCOUNT=>30))),
          agg AS (SELECT ORDER_DATE d, COUNT(*) cnt, MAX(LOADED_AT) lt, {aggcols} FROM {tbl} GROUP BY ORDER_DATE)
          SELECT TO_CHAR(cal.d,'YYYY-MM-DD'), COALESCE(agg.cnt,0), TO_CHAR(agg.lt), {selcols}
          FROM cal LEFT JOIN agg ON cal.d=agg.d ORDER BY cal.d"""
        rows=sf.execute(q).rows
        last_load=None
        for r in rows:
            d, cnt, lt = r[0], int(r[1]), r[2]
            if lt: last_load=lt
            cols_obs=[]
            for i,col in enumerate(SRC_COLS):
                nr=r[3+2*i]; dr=r[4+2*i]
                cols_obs.append({"name":col, "null_rate":float(nr) if nr is not None else 0.0,
                                 **({"distinct_ratio":float(dr)} if dr is not None else {})})
            prof.record_snapshot(ctx, mid, tbl, {"ts":f"{d}T23:00:00","row_count":cnt,"rows_analyzed":cnt,
                "last_load_ts": last_load or f"{d}T06:00:00","columns":cols_obs})
        print(f"  {name[:40]:<40} {len(rows)} days re-backfilled (real null + distinct)")
    print("done")

if __name__=="__main__":
    main()
