"""Recursive range-checksum compare for SAME-DATABASE source↔target (datadiff's chunked
hash approach, made recursive).

When both sides live in the same database (same Snowflake account, same Oracle instance),
we compare WITHOUT bringing data to the app:

  1. Whole-table fast path: one `(COUNT, SUM(HASH(row)))` per side. Equal ⇒ identical, done —
     zero rows moved.
  2. Otherwise descend the numeric key space: split the current range into B equal-width
     buckets and, in ONE pushed-down aggregate per side, get each bucket's (count, checksum).
     Buckets whose (count, checksum) match on both sides are PRUNED (never read). Buckets
     that differ are recursed into; once a differing bucket is small enough it becomes a
     *leaf* — only then do we pull those few rows and run the exact cell-level compare.

So the data that crosses the wire is just per-bucket aggregates plus the rows inside
divergent leaves — not the matching bulk. The row hash folds in the KEY, so a bucket whose
(count, checksum) matches is guaranteed identical. Numeric single key only; other keys fall
back to the per-key hash / row engines upstream.
"""

from __future__ import annotations

import time
from typing import Any

from dvp.testing_engine.cancel import CompareCancelled
from dvp.testing_engine.rowcompare import (
    B_MATCH,
    CompareOptions,
    CompareOutcome,
    CompareSummary,
    RowDiff,
    compare_rows,
)
from dvp.testing_engine.sql_canon import numeric_text, string_text
from dvp.testing_engine.typemap import CanonicalType, canonicalize

_SAME_DB_DIALECTS = {"snowflake", "oracle", "sqlite"}
BRANCHING = 8  # buckets per recursion level


class _StopEarly(Exception):
    """Raised to abort a compare that hit its query/time budget — never descend forever."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def supported(src_conn: Any, dst_conn: Any, key: list[str], options: CompareOptions) -> bool:
    """Usable for an exact, single-numeric-key compare on a same-database dialect. The caller
    is responsible for confirming the two sides are actually the SAME server (see
    `same_server`) before routing here — this only checks the compare is eligible."""
    d = getattr(src_conn, "dialect", None)
    return (
        d is not None
        and d == getattr(dst_conn, "dialect", None)
        and d in _SAME_DB_DIALECTS
        and len(key) == 1
        and float(options.tolerance) == 0.0
    )


def same_server(a: Any, b: Any) -> bool:
    """Do two connectors point at the same physical database/account (so one can query both
    tables without moving data)?"""
    if a is b:
        return True
    fa, fb = getattr(a, "server_fingerprint", None), getattr(b, "server_fingerprint", None)
    if callable(fa) and callable(fb):
        try:
            return fa() == fb()
        except Exception:  # noqa: BLE001
            return False
    return False


def _q(col: str) -> str:
    return '"' + col.replace('"', '""') + '"'


def _norm(dialect: str, col: str, canon: CanonicalType, opts: CompareOptions) -> str:
    """Per-column expression normalized like the in-memory engine, as TEXT for hashing. All
    rendering routes through `sql_canon` so this same-DB engine, snowflake_hash, and the
    cross-engine fingerprint agree byte-for-byte on what a value's canonical text is."""
    q = _q(col)
    if canon == CanonicalType.NUMERIC:
        return numeric_text(dialect, q)
    return string_text(
        dialect, q, ignore_spaces=opts.ignore_spaces,
        case_insensitive=opts.case_insensitive, null_equals_empty=opts.null_equals_empty,
    )


def _row_hash(dialect: str, all_cols: list[str], types: dict, opts: CompareOptions) -> str:
    parts = [
        f"COALESCE({_norm(dialect, c, canonicalize(types.get(c, 'string')), opts)}, '')"
        for c in all_cols
    ]
    concat = " || '|' || ".join(parts)
    if dialect == "snowflake":
        return f"HASH({concat})"
    if dialect == "oracle":
        return f"ORA_HASH({concat})"
    return f"DVHASH({concat})"  # sqlite UDF


def _where(predicate: str | None, extra: str | None) -> str:
    clauses = [c for c in (predicate, extra) if c]
    return (" WHERE " + " AND ".join(f"({c})" for c in clauses)) if clauses else ""


def compare_recursive_hash(
    conn: Any,
    dst_conn: Any,
    *,
    src_from: str,
    dst_from: str,
    src_predicate: str | None,
    dst_predicate: str | None,
    key: list[str],
    columns: list[str],
    column_types: dict[str, str] | None = None,
    options: CompareOptions | None = None,
    leaf_limit: int | None = None,
) -> tuple[CompareOutcome, list[str]]:
    opts = options or CompareOptions()
    types = column_types or {}
    dialect = getattr(conn, "dialect", "sqlite")
    kcol = key[0]
    key_up = {kcol.upper()}
    columns = [c for c in columns if c.upper() not in key_up]
    all_cols = [kcol, *columns]
    qk = _q(kcol)
    rowhash = _row_hash(dialect, all_cols, types, opts)
    leaf = int(leaf_limit or opts.bad_record_limit or 1000)
    notes: list[str] = []
    stats = {"leaf_queries": 0, "rows_pulled": 0, "queries": 0}
    stopped_early = False

    # Budget guard: count every pushdown query and never run past the query cap or wall-clock
    # deadline. This is what keeps a heavily-divergent huge table from issuing hundreds of
    # thousands of recursion queries over many hours.
    def q(sql: str):  # noqa: ANN202
        stats["queries"] += 1
        # User cancel takes effect before any further query — propagates (not a _StopEarly) so
        # the job is marked 'cancelled', not merely stopped-early.
        if opts.should_cancel and opts.should_cancel():
            raise CompareCancelled()
        # The whole-table checksum + key-range min/max (the first 4 queries) are the essential
        # minimum and always run; the budget guards only the (unbounded) descent/leaf phase.
        if stats["queries"] > 4:
            if opts.max_queries and stats["queries"] > opts.max_queries:
                raise _StopEarly(f"query budget exhausted ({opts.max_queries} queries)")
            if opts.deadline_epoch and time.time() >= opts.deadline_epoch:
                raise _StopEarly("time budget exhausted")
        return conn.execute(sql)

    def checksum(from_sql: str, pred: str | None, rng: str | None) -> tuple[int, int]:
        sql = f"SELECT COUNT(*), COALESCE(SUM({rowhash}), 0) FROM {from_sql}{_where(pred, rng)}"
        row = q(sql).rows
        if not row:
            return 0, 0
        return int(row[0][0] or 0), int(row[0][1] or 0)

    # ---- fast path: whole-table checksum ----
    s_total = checksum(src_from, src_predicate, None)
    d_total = checksum(dst_from, dst_predicate, None)
    src_n, dst_n = s_total[0], d_total[0]
    if s_total == d_total:
        summary = CompareSummary(
            src_row_count=src_n,
            dst_row_count=dst_n,
            keys_total=src_n,
            matched_rows=src_n,
            mismatched_rows=0,
            src_only_rows=0,
            dst_only_rows=0,
            ambiguous_rows=0,
            match_rate_pct=100.0,
            per_column_mismatch={},
        )
        notes.append("identical by full-table checksum — no rows read")
        return CompareOutcome(summary=summary, differences=[], passed_sample=[]), notes

    # ---- global key range across both sides ----
    rng_sql = (
        f"SELECT MIN({qk}), MAX({qk}) FROM {src_from}{_where(src_predicate, None)}",
        f"SELECT MIN({qk}), MAX({qk}) FROM {dst_from}{_where(dst_predicate, None)}",
    )
    lo_s, hi_s = q(rng_sql[0]).rows[0]
    lo_d, hi_d = q(rng_sql[1]).rows[0]
    bounds = [b for b in (lo_s, hi_s, lo_d, hi_d) if b is not None]
    if not bounds:
        return _empty(src_n, dst_n), notes
    # Half-open ranges [lo, hi) everywhere (no shared inclusive edges → no double counting);
    # the global upper bound is made exclusive by +1 so the max key is included.
    lo = float(min(x for x in (lo_s, lo_d) if x is not None))
    hi = float(max(x for x in (hi_s, hi_d) if x is not None)) + 1.0

    def rpred(rlo: float, rhi: float) -> str:
        return f"{qk} >= {rlo} AND {qk} < {rhi}"

    def bucket_agg(from_sql: str, pred: str, rlo: float, rhi: float, width: float) -> dict:
        # FLOOR((k - rlo) / width) over the half-open [rlo, rhi); one grouped pushdown per side
        bexpr = (
            f"CAST(MIN(CAST(({qk} - {rlo}) / {width} AS INTEGER), {BRANCHING - 1}) AS INTEGER)"
            if dialect == "sqlite"
            else f"LEAST(FLOOR(({qk} - {rlo}) / {width}), {BRANCHING - 1})"
        )
        sql = (
            f"SELECT {bexpr} AS b, COUNT(*), COALESCE(SUM({rowhash}), 0) "
            f"FROM {from_sql}{_where(pred, rpred(rlo, rhi))} GROUP BY {bexpr}"
        )
        out: dict[int, tuple[int, int]] = {}
        for b, c, h in q(sql).rows:
            out[int(b)] = (int(c or 0), int(h or 0))
        return out

    # ---- descend ----
    matched = 0
    leaves: list[tuple[float, float]] = []
    differences: list[RowDiff] = []
    passed: list[RowDiff] = []
    per_column: dict[str, int] = {}
    agg = {"matched": 0, "value_difference": 0, "source_only": 0, "dst_only": 0, "ambiguous": 0}
    # fail-fast threshold (only when enabled); confirmed differing rows that trip the circuit.
    diff_trip = int(opts.max_diff_rows or 1000) if opts.fail_fast else 0

    def diffs_so_far() -> int:
        return agg["value_difference"] + agg["source_only"] + agg["dst_only"] + agg["ambiguous"]

    try:
        queue: list[tuple[float, float]] = [(lo, hi)]
        guard = 0
        while queue:
            guard += 1
            if guard > 100000:  # pathological safety valve
                notes.append("recursion guard hit — remaining ranges pulled as leaves")
                leaves.extend(queue)
                break
            rlo, rhi = queue.pop()
            width = (rhi - rlo) / BRANCHING
            sc = checksum(src_from, src_predicate, rpred(rlo, rhi))
            dc = checksum(dst_from, dst_predicate, rpred(rlo, rhi))
            if sc == dc and sc[0] > 0:
                matched += sc[0]
                continue
            if max(sc[0], dc[0]) <= leaf or width <= 0 or rhi <= rlo:
                leaves.append((rlo, rhi))
                continue
            sb = bucket_agg(src_from, src_predicate, rlo, rhi, width)
            db = bucket_agg(dst_from, dst_predicate, rlo, rhi, width)
            for b in range(BRANCHING):
                s, d = sb.get(b, (0, 0)), db.get(b, (0, 0))
                if s == d:
                    matched += s[0]  # identical bucket → prune
                    continue
                blo = rlo + b * width
                bhi = rhi if b == BRANCHING - 1 else rlo + (b + 1) * width
                queue.append((blo, bhi))

        # ---- pull only divergent leaves, cell-compare exactly ----
        for rlo, rhi in leaves:
            rng = rpred(rlo, rhi)
            src_rows = _pull(q, src_from, all_cols, src_predicate, rng)
            dst_rows = _pull(q, dst_from, all_cols, dst_predicate, rng)
            stats["leaf_queries"] += 2
            stats["rows_pulled"] += len(src_rows) + len(dst_rows)
            leaf_outcome = compare_rows(
                src_rows, dst_rows, key=key, columns=columns, column_types=types, options=opts
            )
            ls = leaf_outcome.summary
            agg["matched"] += ls.matched_rows
            agg["value_difference"] += ls.mismatched_rows
            agg["source_only"] += ls.src_only_rows
            agg["dst_only"] += ls.dst_only_rows
            agg["ambiguous"] += ls.ambiguous_rows
            for col, n in ls.per_column_mismatch.items():
                per_column[col] = per_column.get(col, 0) + n
            cap = opts.bad_record_limit
            for d in leaf_outcome.differences:
                if len(differences) < cap * 4:
                    differences.append(d)
            for p in leaf_outcome.passed_sample:
                if len(passed) < opts.pass_sample_limit:
                    passed.append(p)
            # Fail-fast: once we've confirmed the tables differ by enough rows, stop pulling
            # the rest of the divergent leaves — the verdict ("they differ a lot") is settled.
            if diff_trip and diffs_so_far() >= diff_trip:
                raise _StopEarly(f"fail-fast: {diffs_so_far()}+ differing rows found")
    except _StopEarly as stop:
        stopped_early = True
        notes.append(
            f"stopped early — {stop.reason}. The tables differ substantially; this is a "
            f"lower-bound sample of the differences, not the full set. Fix the data and re-run, "
            f"widen the budget (options.max_queries / timeout), or set fail_fast=false to force "
            f"a full compare."
        )

    # A run that stopped before confirming anything is INCONCLUSIVE — never report it as a
    # match (a clean sample must not mask an un-examined, differing bulk).
    if stopped_early and (agg["value_difference"] + agg["source_only"] + agg["dst_only"] + agg["ambiguous"]) == 0:
        agg["ambiguous"] = 1

    matched_total = matched + agg["matched"]
    keys_total = (
        matched_total
        + agg["value_difference"]
        + agg["source_only"]
        + agg["dst_only"]
        + agg["ambiguous"]
    )
    match_rate = round(100.0 * matched_total / keys_total, 2) if keys_total else 100.0
    if not stopped_early:
        notes.append(
            f"recursive hash: pulled {stats['rows_pulled']} rows from "
            f"{len(leaves)} divergent leaf range(s); matching ranges pruned in-database"
        )
    summary = CompareSummary(
        src_row_count=src_n,
        dst_row_count=dst_n,
        keys_total=keys_total,
        matched_rows=matched_total,
        mismatched_rows=agg["value_difference"],
        src_only_rows=agg["source_only"],
        dst_only_rows=agg["dst_only"],
        ambiguous_rows=agg["ambiguous"],
        match_rate_pct=match_rate,
        per_column_mismatch=per_column,
        stopped_early=stopped_early,
    )
    return CompareOutcome(summary=summary, differences=differences, passed_sample=passed), notes


def _pull(q, from_sql: str, cols: list[str], pred: str | None, rng: str) -> list[dict]:  # noqa: ANN001
    """`q` is the budget-guarded executor (counts the query, enforces the deadline)."""
    collist = ", ".join(_q(c) for c in cols)
    res = q(f"SELECT {collist} FROM {from_sql}{_where(pred, rng)}")
    return [dict(zip(res.columns, row, strict=False)) for row in res.rows]


def _empty(src_n: int, dst_n: int) -> CompareOutcome:
    return CompareOutcome(
        summary=CompareSummary(
            src_row_count=src_n,
            dst_row_count=dst_n,
            keys_total=0,
            matched_rows=0,
            mismatched_rows=0,
            src_only_rows=0,
            dst_only_rows=0,
            ambiguous_rows=0,
            match_rate_pct=100.0,
            per_column_mismatch={},
        ),
        differences=[],
        passed_sample=[],
    )


_ = (B_MATCH,)  # keep import surface stable for parity with other engines
