"""Functional test: the Stage-0 sample gate flags a systematic column (a date shifted on every row)."""
from __future__ import annotations

from dvp.abstractions.factory import build_container
from dvp.config import get_settings
from dvp.connectors.registry import build_connector
from dvp.db.models import Connection
from dvp.services.tenancy import bootstrap_local_tenant, resolve_context
from dvp.testing_engine import sample_gate
from dvp.testing_engine.rowcompare import CompareOptions

ORA = "16a1a10c-a53f-4937-b73e-8b010330488a"
SF = "3ba56de1-5484-49a6-8f65-a4a71d39babe"
COLS = ["NAME", "EMAIL", "AMOUNT", "STATUS", "CREATED_DT", "REGION", "NOTE"]
TYPES = {"ID": "number", "NAME": "varchar", "EMAIL": "varchar", "AMOUNT": "number",
         "STATUS": "varchar", "CREATED_DT": "date", "REGION": "varchar", "NOTE": "varchar"}


def conn(c, ctx, cid):
    with c.metadata.session(ctx) as s:
        x = s.get(Connection, cid); st, co, a, r = x.source_type, dict(x.coordinates), x.auth_method, x.secret_ref
    return build_connector(st, co, a, c.secrets.get(ctx, r) if r and c.secrets else None)


def main() -> None:
    s = get_settings(); c = build_container(s); c.metadata.create_all()
    t = bootstrap_local_tenant(c.metadata, s)
    ctx = resolve_context(c.metadata, user_id=t.user_id, org_id=t.org_id, group_id=t.group_id)
    ora = conn(c, ctx, ORA); sf = conn(c, ctx, SF)

    # variant: shift EVERY date by 1 day (100% different on CREATED_DT, all else identical)
    sf.execute(
        "CREATE OR REPLACE TABLE DATAQ_BENCH.PUBLIC.DQ_BENCH_1M_DATE AS "
        "SELECT id,name,email,amount,status, DATEADD(day,1,created_dt) AS created_dt, region, note "
        "FROM DATAQ_BENCH.PUBLIC.DQ_BENCH_1M"
    )

    def pull(connector, from_sql, cols, pred=None, limit=None, order_by=None):
        collist = ", ".join('"' + x.replace('"', '""') + '"' for x in cols)
        where = f" WHERE {pred}" if pred else ""
        ob = f" ORDER BY {order_by}" if order_by else ""
        if limit:
            cap = f" FETCH FIRST {int(limit)} ROWS ONLY" if getattr(connector, "dialect", "") == "oracle" else f" LIMIT {int(limit)}"
        else:
            cap = ""
        res = connector.execute(f"SELECT {collist} FROM {from_sql}{where}{ob}{cap}")
        return [dict(zip(res.columns, row)) for row in res.rows]

    for label, tgt in [("identical", "DQ_BENCH_1M"), ("date 100% shifted", "DQ_BENCH_1M_DATE")]:
        g = sample_gate.precheck(
            pull, ora, sf,
            src_from="DATAQ.DQ_BENCH_1M", dst_from=f"DATAQ_BENCH.PUBLIC.{tgt}",
            src_predicate=None, dst_predicate=None,
            key=["ID"], columns=COLS, src_cols=["ID", *COLS], dst_cols=["ID", *COLS],
            column_types=TYPES, options=CompareOptions(),
        )
        print(f"\n=== {label} ===")
        print(f"  decision={g.decision}  common={g.common}  value_diff_rows={g.value_mismatch_rows}")
        print(f"  systematic_columns={g.systematic_columns}")
        print(f"  encoding_suspect_columns={g.encoding_suspect_columns}")
        print(f"  match_rate={ {k: v for k, v in g.column_match_rate.items()} }")
        print(f"  note: {g.note}")


if __name__ == "__main__":
    main()
