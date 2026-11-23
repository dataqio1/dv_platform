"""Benchmark: cross-engine (Oracle↔Snowflake) bucketed HASH compare vs diff rate.

Hypothesis (the user's): as the % of differing rows rises, a hash/bucket compare degrades —
because a bucket is "all-or-nothing", any differing row makes its whole bucket mismatch, forcing
those rows to be re-fetched. With SCATTERED diffs this collapses fast: at 1% diff over 1000-row
buckets, P(a bucket is clean) = 0.99^1000 ≈ 0, so ~every bucket differs → the hash compare must
drill into the ENTIRE table → it's pure overhead on top of a full pull.

This harness measures that empirically against the real engines:
  * Source  : Oracle    DATAQ.DQ_BENCH_1M          (clean, the "truth")
  * Target  : Snowflake DATAQ_BENCH.PUBLIC.DQ_BENCH_1M_<variant>  (0/1/5/10% diffs)
It builds per-bucket (count, SUM(row-hash)) on each engine — pushed down, cross-engine-identical
SHA-256 — and reports, per diff rate: differing buckets, rows that must be re-fetched (the hash
compare's real cost), and the pushdown scan time.

Usage: PYTHONPATH=src DVP_PROFILE=local .venv/bin/python scripts/bench_diff_rates.py
"""

from __future__ import annotations

import time

from dvp.abstractions.factory import build_container
from dvp.config import get_settings
from dvp.connectors.registry import build_connector
from dvp.db.models import Connection
from dvp.services.tenancy import bootstrap_local_tenant, resolve_context

ORACLE_CONN = "16a1a10c-a53f-4937-b73e-8b010330488a"
SNOWFLAKE_CONN = "3ba56de1-5484-49a6-8f65-a4a71d39babe"

ROWS = 1_000_000
ORA_SRC = "DATAQ.DQ_BENCH_1M"
SF_BASE = "DATAQ_BENCH.PUBLIC.DQ_BENCH_1M"

# Canonical row payload (same logical text on both engines → same SHA-256 for equal rows).
ORA_PAYLOAD = (
    "id || '|' || name || '|' || email || '|' || "
    "RTRIM(RTRIM(TO_CHAR(CAST(amount AS NUMBER(38,10)),'FM99999999999999999990.0000000000'),'0'),'.') || '|' || "
    "status || '|' || TO_CHAR(created_dt,'YYYY-MM-DD') || '|' || region || '|' || note"
)
SF_PAYLOAD = (
    "id || '|' || name || '|' || email || '|' || "
    "RTRIM(RTRIM(TO_VARCHAR(CAST(amount AS NUMBER(38,10))),'0'),'.') || '|' || "
    "status || '|' || TO_VARCHAR(created_dt,'YYYY-MM-DD') || '|' || region || '|' || note"
)
# Per-row numeric fold of the SHA-256 → one integer, summed per bucket (order-independent).
# Snowflake has no CONV, so convert the first N hex nibbles to an int with a positional sum built
# from functions BOTH engines share (Oracle INSTR / Snowflake POSITION). N=12 → value < 16^12,
# SUM over 1M rows < 10^21, fits NUMBER(38) on both.
_HEX = "0123456789abcdef"


def _vexpr(hash_hex: str, dialect: str, n: int = 12) -> str:
    terms = []
    for k in range(1, n + 1):
        ch = f"SUBSTR({hash_hex},{k},1)"
        dig = f"(INSTR('{_HEX}',{ch})-1)" if dialect == "oracle" else f"(POSITION({ch} IN '{_HEX}')-1)"
        terms.append(f"{dig}*{16 ** (n - k)}")
    return "(" + " + ".join(terms) + ")"


ORA_V = _vexpr(f"LOWER(STANDARD_HASH({ORA_PAYLOAD},'SHA256'))", "oracle")
SF_V = _vexpr(f"SHA2({SF_PAYLOAD},256)", "snowflake")

# Diff variants (scattered uniformly, plus one clustered to contrast). nth=100 → every 100th row.
VARIANTS = [
    ("0% (identical)", "DQ_BENCH_1M", None),
    ("1% scattered", "DQ_BENCH_1M_D01", ("MOD(id,100)=0", "every 100th row")),
    ("5% scattered", "DQ_BENCH_1M_D05", ("MOD(id,20)=0", "every 20th row")),
    ("10% scattered", "DQ_BENCH_1M_D10", ("MOD(id,10)=0", "every 10th row")),
    ("10% clustered", "DQ_BENCH_1M_C10", ("id<=100000", "one contiguous block")),
]
BUCKETS = 1000  # 1000 rows/bucket at 1M
WIDTH = ROWS // BUCKETS  # key-range width per bucket (the recursive compare splits key RANGES,
# not residues — a contiguous diff block then lands in few buckets, while scattered diffs hit all)


def get_connector(container, ctx, conn_id):
    with container.metadata.session(ctx) as s:
        c = s.get(Connection, conn_id)
        st, coords, auth, ref = c.source_type, dict(c.coordinates), c.auth_method, c.secret_ref
    secret = container.secrets.get(ctx, ref) if ref and container.secrets else None
    return build_connector(st, coords, auth, secret)


def bucket_map(conn, table, vexpr, width):
    """{bucket: (count, checksum)} via one pushed-down aggregate, bucketed by KEY RANGE
    (FLOOR((id-1)/width)) so contiguous diffs localize. Returns (map, seconds)."""
    bkt = f"FLOOR((id-1)/{width})"
    sql = (
        f"SELECT {bkt} AS bkt, COUNT(*) AS cnt, SUM({vexpr}) AS chk "
        f"FROM {table} GROUP BY {bkt}"
    )
    t = time.time()
    res = conn.execute(sql)
    dt = time.time() - t
    idx = {c.upper(): i for i, c in enumerate(res.columns)}
    m = {int(r[idx["BKT"]]): (int(r[idx["CNT"]]), str(r[idx["CHK"]])) for r in res.rows}
    return m, dt


def main() -> None:
    settings = get_settings()
    container = build_container(settings)
    container.metadata.create_all()
    t = bootstrap_local_tenant(container.metadata, settings)
    ctx = resolve_context(container.metadata, user_id=t.user_id, org_id=t.org_id, group_id=t.group_id)
    ora = get_connector(container, ctx, ORACLE_CONN)
    sf = get_connector(container, ctx, SNOWFLAKE_CONN)

    # 1) Build Snowflake diff variants (idempotent).
    print("Building Snowflake diff variants…")
    for _, name, mut in VARIANTS:
        if mut is None:
            continue  # 0% uses the base table
        pred, _desc = mut
        sf.execute(
            f"CREATE OR REPLACE TABLE DATAQ_BENCH.PUBLIC.{name} AS "
            f"SELECT id, name, email, amount, status, created_dt, region, "
            f"CASE WHEN {pred} THEN note || ' [MODIFIED]' ELSE note END AS note "
            f"FROM {SF_BASE}"
        )
    print("  done.\n")

    # 2) Oracle source bucket map — computed ONCE, reused for every variant.
    print(f"Scanning Oracle source ({ROWS:,} rows, {BUCKETS} buckets)…")
    src_map, src_dt = bucket_map(ora, ORA_SRC, ORA_V, WIDTH)
    print(f"  Oracle bucket scan: {src_dt:.1f}s\n")

    # 3) Per-variant: compare bucket maps.
    print(f"{'diff rate':<16} {'diff buckets':>13} {'rows to refetch':>16} {'% of table':>11} {'sf scan':>8} {'verdict'}")
    print("-" * 86)
    for label, name, _mut in VARIANTS:
        table = f"DATAQ_BENCH.PUBLIC.{name}"
        tgt_map, tgt_dt = bucket_map(sf, table, SF_V, WIDTH)
        diff_buckets = 0
        refetch = 0
        for b in set(src_map) | set(tgt_map):
            s_cnt, s_chk = src_map.get(b, (0, "0"))
            t_cnt, t_chk = tgt_map.get(b, (0, "0"))
            if s_cnt != t_cnt or s_chk != t_chk:
                diff_buckets += 1
                refetch += max(s_cnt, t_cnt)
        pct = 100.0 * refetch / ROWS
        # Hash wins only while it avoids most of the data; once ~all buckets differ it's a full
        # pull PLUS the bucket overhead → stop doing hash compare.
        verdict = "hash WINS" if pct < 50 else "hash LOSES → full pull"
        print(f"{label:<16} {diff_buckets:>8}/{BUCKETS:<4} {refetch:>16,} {pct:>10.1f}% {tgt_dt:>7.1f}s  {verdict}")

    print("-" * 86)
    print(
        "\nReading: 'rows to refetch' is what the hash compare must still pull to localize diffs.\n"
        "When it approaches the whole table, bucketing bought nothing — switch to a straight\n"
        "streaming/full compare. Note how SCATTERED diffs blow past that line almost immediately\n"
        "(0.99^1000 ≈ 0 → every 1000-row bucket is dirty at just 1%), while a CLUSTERED 10% stays\n"
        "cheap. The crossover is driven by diff *dispersion*, not just diff %."
    )


if __name__ == "__main__":
    main()
