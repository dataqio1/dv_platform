"""Cross-database fingerprint compare — the bandwidth-optimal fast path for mostly-equal large
tables across DIFFERENT engines (e.g. Oracle↔Snowflake).

It's the same proven range-checksum bisection as the same-database ``recursive_hash`` engine,
generalized two ways so it works across engines:

  * each side is queried through its OWN connector (no "same server" assumption), and
  * the per-row checksum is a CROSS-ENGINE one — ``MD5`` over a normalized canonical string,
    folded to the SAME 32-bit integer on every dialect (sqlite ``DVHASH``, Snowflake ``MD5``,
    Oracle ``STANDARD_HASH``). ``HASH``/``ORA_HASH`` are deliberately NOT used — they disagree
    across engines; a standard ``MD5`` does not.

Flow: a whole-table ``(COUNT, SUM(rowhash))`` per side (equal ⇒ identical, zero rows moved);
otherwise descend the numeric key space in buckets, pruning any bucket whose ``(count, checksum)``
matches on BOTH sides, and only pulling the rows inside divergent leaves — which are then compared
with the authoritative in-memory ``compare_rows``. So a normalization quirk that makes equal rows
look different only costs an extra leaf pull (the leaf compare is the judge), never a wrong verdict.

v1: single numeric key (exact key-range bucketing), tolerance 0, both dialects fingerprint-able.
"""

from __future__ import annotations

import time
from typing import Any

from dvp.testing_engine.cancel import CompareCancelled
from dvp.testing_engine.recursive_hash import (
    BRANCHING,
    _q,
    _StopEarly,
    _where,
)
from dvp.testing_engine.rowcompare import (
    CompareOptions,
    CompareOutcome,
    CompareSummary,
    RowDiff,
    compare_rows,
)
from dvp.testing_engine.sql_canon import (
    boolean_text,
    numeric_text,
    string_text,
    temporal_text,
)
from dvp.testing_engine.typemap import CanonicalType, canonicalize

# Dialects where we can emit an MD5-based row checksum that folds to the SAME integer as the
# sqlite DVHASH UDF (int.from_bytes(md5(s)[:4], "big") - 2**31).
FINGERPRINT_DIALECTS = {"sqlite", "snowflake", "oracle"}

# The first pass breaks the key range into chunks of ≈ this many rows, so a huge table is processed
# incrementally (bounded per-query work, live progress, early bail) instead of one giant scan. Small
# tables collapse to a single chunk — one checksum per side, identical ⇒ done. The number of chunks
# also IS the dirty-fraction gate: too many dirty chunks ⇒ hashing can't prune ⇒ bail to streaming.
CHUNK_ROWS = 100_000
MAX_CHUNKS = 512
# A dirty chunk this size or smaller is pulled DIRECTLY (one range read per side + authoritative
# cell-compare) rather than recursively bisected — because chunks are already bounded to ≈CHUNK_ROWS,
# bisecting a fully-dirty chunk is a pointless query storm (it can't prune). Only an unusually large
# chunk (skewed key distribution) falls back to bisection.
DIRECT_PULL_ROWS = 250_000


class BailToStreaming(Exception):
    """Raised when the level-0 gate finds too much divergence for bisection to pay off. The caller
    catches this and falls back to the streaming engine."""

    def __init__(self, dirty_fraction: float, buckets: int) -> None:
        super().__init__(f"{dirty_fraction:.0%} of {buckets} key-range buckets diverge")
        self.dirty_fraction = dirty_fraction


def _xnorm(dialect: str, col: str, canon: CanonicalType, opts: CompareOptions) -> str:
    """Canonical TEXT for a column that is BYTE-IDENTICAL across oracle↔snowflake for equal logical
    values — so the fingerprint prunes equal rows instead of over-drilling. (The default ``_norm``
    renders numbers/dates differently per engine: 1.5 vs 1.5E+000, 2020-01-01 vs 01-JAN-20.)"""
    q = _q(col)
    if canon == CanonicalType.NUMERIC:
        return numeric_text(dialect, q)  # shared exact canonical (no float round-trip)
    if canon in (CanonicalType.DATE, CanonicalType.TIMESTAMP):
        return temporal_text(dialect, q, is_date=(canon == CanonicalType.DATE))
    if canon == CanonicalType.BOOLEAN:
        return boolean_text(dialect, q)
    # STRING / UNKNOWN → text, with the same options the in-memory engine applies.
    return string_text(
        dialect, q, ignore_spaces=opts.ignore_spaces,
        case_insensitive=opts.case_insensitive, null_equals_empty=opts.null_equals_empty,
    )


def supported(src_conn: Any, dst_conn: Any, key: list[str], options: CompareOptions) -> bool:
    """Exact, single-numeric-key compare where BOTH dialects can compute the cross-engine MD5
    checksum. (Same-server same-dialect pairs are better served by recursive_hash upstream; this is
    for the cross-engine case, but it's correct either way.)"""
    ds = getattr(src_conn, "dialect", None)
    dd = getattr(dst_conn, "dialect", None)
    return (
        ds in FINGERPRINT_DIALECTS
        and dd in FINGERPRINT_DIALECTS
        and len(key) == 1
        and float(options.tolerance) == 0.0
    )


def _rowhash_expr(dialect: str, all_cols: list[str], types: dict, opts: CompareOptions) -> str:
    """A per-row checksum expression that yields the SAME signed 32-bit int on every dialect for an
    identical normalized row — so two different engines agree when the data matches."""
    concat = " || '|' || ".join(
        f"COALESCE({_xnorm(dialect, c, canonicalize(types.get(c, 'string')), opts)}, '')"
        for c in all_cols
    )
    if dialect == "sqlite":
        return f"DVHASH({concat})"  # UDF: int.from_bytes(md5(s)[:4],'big') - 2**31
    if dialect == "snowflake":
        return f"(TO_NUMBER(SUBSTR(MD5({concat}), 1, 8), 'XXXXXXXX') - 2147483648)"
    if dialect == "oracle":
        return (
            f"(TO_NUMBER(SUBSTR(RAWTOHEX(STANDARD_HASH({concat}, 'MD5')), 1, 8), 'XXXXXXXX') "
            f"- 2147483648)"
        )
    raise ValueError(f"no cross-engine fingerprint for dialect {dialect!r}")


def compare_fingerprint(
    src_conn: Any,
    dst_conn: Any,
    *,
    src_from: str,
    dst_from: str,
    src_predicate: str | None,
    dst_predicate: str | None,
    key: list[str],
    columns: list[str],
    dst_columns: list[str] | None = None,
    column_mappings: dict | None = None,
    column_types: dict[str, str] | None = None,
    options: CompareOptions | None = None,
    leaf_limit: int | None = None,
    bail_pct: float = 0.6,
    progress: Any = None,
    log: Any = None,
) -> tuple[CompareOutcome, list[str]]:
    opts = options or CompareOptions()
    types = column_types or {}
    _log = log or (lambda *_a, **_k: None)
    ds = getattr(src_conn, "dialect", "sqlite")
    dd = getattr(dst_conn, "dialect", "sqlite")
    kcol = key[0]
    key_up = {kcol.upper()}
    columns = [c for c in columns if c.upper() not in key_up]
    src_all = [kcol, *columns]
    # Target columns may be renamed; the checksum must run over the SAME logical columns on the
    # target side, in the same order, so an identical row folds to the same hash.
    dst_cols = dst_columns or columns
    dst_key = (column_mappings or {}).get(kcol, kcol) if column_mappings else kcol
    dst_all = [dst_key, *dst_cols]
    # Target types keyed by the TARGET column name (the normalizer runs in the target DB).
    dst_types = {d: types.get(s, "string") for s, d in zip(src_all, dst_all, strict=False)}

    qk_s, qk_d = _q(kcol), _q(dst_key)
    src_hash = _rowhash_expr(ds, src_all, types, opts)
    dst_hash = _rowhash_expr(dd, dst_all, dst_types, opts)
    leaf = int(leaf_limit or opts.bad_record_limit or 1000)
    notes: list[str] = []
    stats = {"queries": 0, "leaf_queries": 0, "rows_pulled": 0, "pruned_rows": 0}

    def _mk_q(conn: Any):  # noqa: ANN202
        def q(sql: str):  # noqa: ANN202
            stats["queries"] += 1
            if opts.should_cancel and opts.should_cancel():
                raise CompareCancelled()
            if stats["queries"] > 6:
                if opts.max_queries and stats["queries"] > opts.max_queries:
                    raise _StopEarly(f"query budget exhausted ({opts.max_queries} queries)")
                if opts.deadline_epoch and time.time() >= opts.deadline_epoch:
                    raise _StopEarly("time budget exhausted")
            return conn.execute(sql)
        return q

    qsrc, qdst = _mk_q(src_conn), _mk_q(dst_conn)

    def checksum(q, from_sql, rowhash, pred, rng) -> tuple[int, int]:  # noqa: ANN001
        row = q(f"SELECT COUNT(*), COALESCE(SUM({rowhash}), 0) FROM {from_sql}{_where(pred, rng)}").rows
        return (int(row[0][0] or 0), int(row[0][1] or 0)) if row else (0, 0)

    def rpred(qk: str, rlo: float, rhi: float) -> str:
        return f"{qk} >= {rlo} AND {qk} < {rhi}"

    # (No separate whole-table checksum — the chunked first pass below subsumes it: a single-chunk
    #  table does one checksum per side, identical ⇒ done; a large table is chunked by size.)

    def bucket_agg(q, from_sql, rowhash, qk, pred, rlo, rhi, width) -> dict:  # noqa: ANN001
        bexpr = (
            f"MIN(CAST(({qk} - {rlo}) / {width} AS INTEGER), {BRANCHING - 1})"
            if getattr(q, "is_sqlite", False)
            else f"LEAST(CAST(({qk} - {rlo}) / {width} AS INTEGER), {BRANCHING - 1})"
        )
        sql = (
            f"SELECT {bexpr} AS b, COUNT(*), COALESCE(SUM({rowhash}), 0) "
            f"FROM {from_sql}{_where(pred, rpred(qk, rlo, rhi))} GROUP BY {bexpr}"
        )
        return {int(b): (int(c or 0), int(h or 0)) for b, c, h in q(sql).rows}

    # SQLite lacks LEAST; it uses MIN(a, b) as a scalar instead. Flag each executor so bucket_agg
    # (used when bisecting dirty chunks) picks the right clamp function per dialect.
    qsrc.is_sqlite = ds == "sqlite"  # type: ignore[attr-defined]
    qdst.is_sqlite = dd == "sqlite"  # type: ignore[attr-defined]

    # ---- row counts (cheap, no hashing) — size the pass + catch a both-empty table early ----
    src_n = int(qsrc(f"SELECT COUNT(*) FROM {src_from}{_where(src_predicate, None)}").rows[0][0] or 0)
    dst_n = int(qdst(f"SELECT COUNT(*) FROM {dst_from}{_where(dst_predicate, None)}").rows[0][0] or 0)
    if src_n == 0 and dst_n == 0:
        return _all_match(0, 0), notes

    # ---- global key range across both sides ----
    lo_s, hi_s = qsrc(f"SELECT MIN({qk_s}), MAX({qk_s}) FROM {src_from}{_where(src_predicate, None)}").rows[0]
    lo_d, hi_d = qdst(f"SELECT MIN({qk_d}), MAX({qk_d}) FROM {dst_from}{_where(dst_predicate, None)}").rows[0]
    los = [x for x in (lo_s, lo_d) if x is not None]
    his = [x for x in (hi_s, hi_d) if x is not None]
    if not los or not his:
        return _all_match(src_n, dst_n), notes
    lo, hi = float(min(los)), float(max(his)) + 1.0

    # ---- size-adaptive chunked first pass ----
    # Break the key range into chunks of ≈ CHUNK_ROWS. Each chunk: one checksum per side. A chunk
    # that matches is pruned (never read); a chunk that differs is queued for bisection. The running
    # dirty-chunk fraction IS the gate — once it can't fall below ``bail_pct`` we abort to streaming
    # (scattered diffs dirty every chunk, so pruning buys nothing). Small table ⇒ one chunk ⇒ this is
    # exactly the old "identical by whole-table checksum" fast path, with progress for free.
    pruned_matched = 0
    nchunks = max(1, min(MAX_CHUNKS, -(-max(src_n, dst_n) // CHUNK_ROWS)))
    cwidth = (hi - lo) / nchunks
    gate_on = nchunks >= 4  # a dirty FRACTION only means something with enough chunks
    gate_min = min(nchunks, 8)
    dirty = 0
    direct_leaves: list[tuple[float, float]] = []  # dirty & bounded → pull directly
    bisect_ranges: list[tuple[float, float]] = []  # dirty & oversized (key skew) → bisect
    for i in range(nchunks):
        clo = lo + i * cwidth
        chi = hi if i == nchunks - 1 else lo + (i + 1) * cwidth
        sc = checksum(qsrc, src_from, src_hash, src_predicate, rpred(qk_s, clo, chi))
        dc = checksum(qdst, dst_from, dst_hash, dst_predicate, rpred(qk_d, clo, chi))
        if sc == dc:
            pruned_matched += sc[0]
            stats["pruned_rows"] += sc[0]
        else:
            dirty += 1
            (direct_leaves if max(sc[0], dc[0]) <= DIRECT_PULL_ROWS else bisect_ranges).append((clo, chi))
        if progress:
            try:
                progress(i + 1, nchunks)  # (processed, total) — same contract the streaming engine uses
            except Exception:  # noqa: BLE001 — progress is best-effort, never fail the compare
                pass
        # Early bail: `dirty` only grows, so dirty/nchunks is a lower bound on the final fraction —
        # once it passes the threshold we can stop scanning and stream instead.
        if gate_on and (i + 1) >= gate_min and dirty / nchunks > bail_pct:
            frac = dirty / nchunks
            _log(f"Fingerprint gate: {dirty}/{i + 1} chunks dirty (≥{frac:.0%} > {bail_pct:.0%}) — bailing to streaming")
            raise BailToStreaming(frac, nchunks)
    if dirty == 0:
        notes.append(f"identical by chunked checksum ({nchunks} chunk(s)) — no rows read")
        return _all_match(src_n, dst_n), notes
    _log(
        f"Fingerprint: {nchunks} chunk(s) scanned, {dirty} dirty ({dirty / nchunks:.0%}) — "
        f"{len(direct_leaves)} pulled directly, {len(bisect_ranges)} bisected"
    )

    agg = {"matched": 0, "value_difference": 0, "source_only": 0, "dst_only": 0, "ambiguous": 0}
    per_column: dict[str, int] = {}
    differences: list[RowDiff] = []
    passed: list[RowDiff] = []
    leaves: list[tuple[float, float]] = list(direct_leaves)
    diff_trip = int(opts.max_diff_rows or 1000) if opts.fail_fast else 0
    stopped_early = False

    def diffs_so_far() -> int:
        return agg["value_difference"] + agg["source_only"] + agg["dst_only"] + agg["ambiguous"]

    try:
        queue: list[tuple[float, float]] = list(bisect_ranges)
        guard = 0
        while queue:
            guard += 1
            if guard > 100_000:
                notes.append("recursion guard hit — remaining ranges pulled as leaves")
                leaves.extend(queue)
                break
            rlo, rhi = queue.pop()
            width = (rhi - rlo) / BRANCHING
            sc = checksum(qsrc, src_from, src_hash, src_predicate, rpred(qk_s, rlo, rhi))
            dc = checksum(qdst, dst_from, dst_hash, dst_predicate, rpred(qk_d, rlo, rhi))
            if sc == dc and sc[0] > 0:
                pruned_matched += sc[0]
                stats["pruned_rows"] += sc[0]
                continue
            if max(sc[0], dc[0]) <= leaf or width <= 0 or rhi <= rlo:
                leaves.append((rlo, rhi))
                continue
            sb = bucket_agg(qsrc, src_from, src_hash, qk_s, src_predicate, rlo, rhi, width)
            db = bucket_agg(qdst, dst_from, dst_hash, qk_d, dst_predicate, rlo, rhi, width)
            for b in range(BRANCHING):
                s, d = sb.get(b, (0, 0)), db.get(b, (0, 0))
                if s == d:
                    pruned_matched += s[0]
                    stats["pruned_rows"] += s[0]
                    continue
                blo = rlo + b * width
                bhi = rhi if b == BRANCHING - 1 else rlo + (b + 1) * width
                queue.append((blo, bhi))

        # ---- authoritative pull + cell-compare of divergent leaves only ----
        for rlo, rhi in leaves:
            src_rows = _pull(qsrc, src_from, src_all, src_predicate, rpred(qk_s, rlo, rhi))
            dst_raw = _pull(qdst, dst_from, dst_all, dst_predicate, rpred(qk_d, rlo, rhi))
            dst_rows = (
                [{dict(zip(dst_all, src_all, strict=False)).get(k2, k2): v for k2, v in r.items()}
                 for r in dst_raw]
                if column_mappings
                else dst_raw
            )
            stats["leaf_queries"] += 2
            stats["rows_pulled"] += len(src_rows) + len(dst_rows)
            out = compare_rows(src_rows, dst_rows, key=key, columns=columns, column_types=types, options=opts)
            ls = out.summary
            agg["matched"] += ls.matched_rows
            agg["value_difference"] += ls.mismatched_rows
            agg["source_only"] += ls.src_only_rows
            agg["dst_only"] += ls.dst_only_rows
            agg["ambiguous"] += ls.ambiguous_rows
            for col, n in ls.per_column_mismatch.items():
                per_column[col] = per_column.get(col, 0) + n
            for d in out.differences:
                if len(differences) < opts.bad_record_limit * 4:
                    differences.append(d)
            for p in out.passed_sample:
                if len(passed) < opts.pass_sample_limit:
                    passed.append(p)
            if diff_trip and diffs_so_far() >= diff_trip:
                raise _StopEarly(f"fail-fast: {diffs_so_far()}+ differing rows found")
    except _StopEarly as stop:
        stopped_early = True
        notes.append(
            f"stopped early — {stop.reason}. The tables differ substantially; this is a lower-bound "
            f"sample, not the full set. Widen the budget or set fail_fast=false for a full compare."
        )

    if stopped_early and diffs_so_far() == 0:
        agg["ambiguous"] = 1  # stopped before confirming anything → inconclusive, never "match"

    matched_total = pruned_matched + agg["matched"]
    keys_total = matched_total + diffs_so_far()
    match_rate = round(100.0 * matched_total / keys_total, 2) if keys_total else 100.0
    if not stopped_early:
        notes.append(
            f"fingerprint compare: pruned {stats['pruned_rows']:,} matching rows in-database; "
            f"pulled {stats['rows_pulled']:,} rows from {len(leaves)} divergent leaf range(s)"
        )
    _log(
        f"Fingerprint: {stats['queries']} checksum queries · pruned {stats['pruned_rows']:,} rows · "
        f"pulled {stats['rows_pulled']:,} from {len(leaves)} divergent leaves"
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
    collist = ", ".join(_q(c) for c in cols)
    res = q(f"SELECT {collist} FROM {from_sql}{_where(pred, rng)}")
    return [dict(zip(res.columns, row, strict=False)) for row in res.rows]


def _all_match(src_n: int, dst_n: int) -> CompareOutcome:
    return CompareOutcome(
        summary=CompareSummary(
            src_row_count=src_n, dst_row_count=dst_n, keys_total=src_n,
            matched_rows=src_n, mismatched_rows=0, src_only_rows=0, dst_only_rows=0,
            ambiguous_rows=0, match_rate_pct=100.0, per_column_mismatch={},
        ),
        differences=[], passed_sample=[],
    )
