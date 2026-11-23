"""Functional test: the fingerprint engine's cross-engine canonical + adaptive dirty-bucket gate,
run directly against the live benchmark tables (Oracle DATAQ.DQ_BENCH_1M vs Snowflake variants).

Expect:
  * 0% identical  → identical by full-table checksum (zero rows pulled) — proves the new _xnorm makes
                    numeric/date columns agree cross-engine.
  * 10% clustered → bisection prunes, pulls only the dirty leaves, reports ~100k diffs, NO bail.
  * 5% scattered  → gate finds ~all buckets dirty → BailToStreaming raised.
"""

from __future__ import annotations

import time

from dvp.abstractions.factory import build_container
from dvp.config import get_settings
from dvp.connectors.registry import build_connector
from dvp.db.models import Connection
from dvp.services.tenancy import bootstrap_local_tenant, resolve_context
from dvp.testing_engine import fingerprint_compare
from dvp.testing_engine.rowcompare import CompareOptions

ORACLE_CONN = "16a1a10c-a53f-4937-b73e-8b010330488a"
SNOWFLAKE_CONN = "3ba56de1-5484-49a6-8f65-a4a71d39babe"
COLUMNS = ["NAME", "EMAIL", "AMOUNT", "STATUS", "CREATED_DT", "REGION", "NOTE"]
TYPES = {
    "ID": "number", "NAME": "varchar", "EMAIL": "varchar", "AMOUNT": "number",
    "STATUS": "varchar", "CREATED_DT": "date", "REGION": "varchar", "NOTE": "varchar",
}


def conn(container, ctx, cid):
    with container.metadata.session(ctx) as s:
        c = s.get(Connection, cid)
        st, co, a, r = c.source_type, dict(c.coordinates), c.auth_method, c.secret_ref
    sec = container.secrets.get(ctx, r) if r and container.secrets else None
    return build_connector(st, co, a, sec)


def main() -> None:
    settings = get_settings()
    container = build_container(settings)
    container.metadata.create_all()
    t = bootstrap_local_tenant(container.metadata, settings)
    ctx = resolve_context(container.metadata, user_id=t.user_id, org_id=t.org_id, group_id=t.group_id)
    ora = conn(container, ctx, ORACLE_CONN)
    sf = conn(container, ctx, SNOWFLAKE_CONN)
    opts = CompareOptions(tolerance=0.0, fail_fast=False)

    cases = [
        ("0% identical", "DATAQ_BENCH.PUBLIC.DQ_BENCH_1M"),
        ("10% clustered", "DATAQ_BENCH.PUBLIC.DQ_BENCH_1M_C10"),
        ("5% scattered", "DATAQ_BENCH.PUBLIC.DQ_BENCH_1M_D05"),
    ]
    for label, tgt in cases:
        print(f"\n=== {label}  (Oracle DATAQ.DQ_BENCH_1M vs Snowflake {tgt.split('.')[-1]}) ===")
        t0 = time.time()
        try:
            outcome, notes = fingerprint_compare.compare_fingerprint(
                ora, sf,
                src_from="DATAQ.DQ_BENCH_1M", dst_from=tgt,
                src_predicate=None, dst_predicate=None,
                key=["ID"], columns=COLUMNS, dst_columns=COLUMNS,
                column_types=TYPES, options=opts,
                log=lambda *a, **k: print("   ·", *a),
            )
            s = outcome.summary
            print(f"   → {time.time()-t0:.1f}s  matched={s.matched_rows:,}  value_diff={s.mismatched_rows:,}  "
                  f"src_only={s.src_only_rows:,}  dst_only={s.dst_only_rows:,}")
            for n in notes:
                print("   note:", n)
        except fingerprint_compare.BailToStreaming as b:
            print(f"   → {time.time()-t0:.1f}s  BAILED TO STREAMING: {b}  ✅ (gate worked)")


if __name__ == "__main__":
    main()
