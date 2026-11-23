"""Snowflake→Snowflake hash-pushdown compare — maximum efficiency, same four buckets.

When BOTH sides are Snowflake and the compare is exact (no numeric tolerance), we never
move matching rows across the wire. Instead we push the row hashing INTO Snowflake,
mirroring datadiff's checksum strategy (`DataCompareUtil.createShaChecksumDF`) but keyed
so we can still locate and bucket differences:

  Phase 1 (pushdown): on each side run
      SELECT <key>, COUNT(*) __CNT, MIN(__RH) __MN, MAX(__RH) __MX
      FROM (SELECT <key>, HASH(<normalized payload…>) __RH FROM <t>) GROUP BY <key>
  and pull only the compact per-key `(key, count, hash)` tuples. Snowflake's native
  `HASH(a, b, …)` is type- and null-aware; we fold the same normalizations the in-memory
  engine applies (TRIM / LOWER / NULLIF '') straight into SQL so verdicts agree. For a 1:1
  key, MIN==MAX==the row hash; count>1 on either side flags the ambiguous bucket.

  Phase 2 (bounded detail): for only a capped sample of differing keys, pull the full rows
  (row-constructor `IN`) and run the pure cell compare to surface column-level diffs. If
  that detail fetch fails, we degrade to key-only detail and surface a warning — never
  fail the compare and never move the whole table.

Counts (matched / mismatched / source-only / target-only / ambiguous) are always exact and
full-table; only the per-column breakdown and cell samples are over the bounded sample
(flagged in the summary warnings).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from dvp.testing_engine.rowcompare import (
    _STATUS_OF,
    B_AMBIGUOUS,
    B_SOURCE_ONLY,
    B_TARGET_ONLY,
    B_VALUE_DIFF,
    CellDiff,
    CompareOptions,
    CompareOutcome,
    CompareSummary,
    RowDiff,
)
from dvp.testing_engine.sql_canon import numeric_text, string_text
from dvp.testing_engine.typemap import CanonicalType, canonicalize, values_equal


def supported(src_conn: Any, dst_conn: Any, options: CompareOptions) -> bool:
    """Hash pushdown is usable only when both sides are Snowflake and the compare is
    exact — a numeric tolerance can't be expressed as an equality hash."""
    return (
        getattr(src_conn, "dialect", None) == "snowflake"
        and getattr(dst_conn, "dialect", None) == "snowflake"
        and float(options.tolerance) == 0.0
    )


def _q(col: str) -> str:
    return '"' + col.replace('"', '""') + '"'


def _norm_expr(col: str, canon: CanonicalType, opts: CompareOptions) -> str:
    """SQL expression that normalizes a column the same way the in-memory engine does,
    so a pushed-down HASH agrees with `values_equal`."""
    e = _q(col)
    if canon == CanonicalType.NUMERIC:
        # Exact canonical text (no DOUBLE round-trip) so distinct large NUMBER ids/amounts don't
        # collapse to the same hash and get wrongly counted as matched (this path has no leaf
        # re-judge for matched rows).
        return numeric_text("snowflake", e)
    return string_text(
        "snowflake", e, ignore_spaces=opts.ignore_spaces,
        case_insensitive=opts.case_insensitive, null_equals_empty=opts.null_equals_empty,
    )


def _agg_sql(from_sql: str, predicate: str | None, key: list[str], payload: list[str]) -> str:
    keysel = ", ".join(_q(k) for k in key)
    hash_expr = f"HASH({', '.join(payload)})" if payload else "HASH(1)"
    where = f" WHERE {predicate}" if predicate else ""
    inner = f"SELECT {keysel}, {hash_expr} AS __RH FROM {from_sql}{where}"
    return (
        f"SELECT {keysel}, COUNT(*) AS __CNT, MIN(__RH) AS __MN, MAX(__RH) AS __MX "
        f"FROM ({inner}) GROUP BY {keysel}"
    )


def _knorm(vals: tuple) -> tuple:
    """Normalize a key tuple for cross-side matching (int 1 / Decimal('1') / '1' align)."""
    return tuple(None if v is None else str(v) for v in vals)


def _pull_aggregate(
    conn: Any, from_sql: str, predicate: str | None, key: list[str], payload: list[str]
) -> dict[tuple, tuple]:
    res = conn.execute(_agg_sql(from_sql, predicate, key, payload))
    idx = {c.upper(): i for i, c in enumerate(res.columns)}
    kpos = [idx[k.upper()] for k in key]
    cpos, mnpos, mxpos = idx["__CNT"], idx["__MN"], idx["__MX"]
    out: dict[tuple, tuple] = {}
    for row in res.rows:
        raw = tuple(row[p] for p in kpos)
        # (count, 1:1 hash or None, raw key tuple for phase-2 literals)
        cnt = int(row[cpos])
        h = row[mnpos] if row[mnpos] == row[mxpos] else None
        out[_knorm(raw)] = (cnt, h, raw)
    return out


def _lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    if isinstance(v, (datetime, date)):
        return "'" + str(v) + "'"
    return "'" + str(v).replace("'", "''") + "'"


def _fetch_rows_by_key(
    conn: Any,
    from_sql: str,
    predicate: str | None,
    all_cols: list[str],
    key: list[str],
    raw_keys: list[tuple],
) -> dict[tuple, dict]:
    if not raw_keys:
        return {}
    cols_sql = ", ".join(_q(c) for c in all_cols)
    kc = ", ".join(_q(k) for k in key)
    tuples = ", ".join("(" + ", ".join(_lit(v) for v in kv) + ")" for kv in raw_keys)
    where = f"({kc}) IN ({tuples})"
    if predicate:
        where = f"({predicate}) AND {where}"
    res = conn.execute(f"SELECT {cols_sql} FROM {from_sql} WHERE {where}")
    out: dict[tuple, dict] = {}
    for row in res.rows:
        rec = dict(zip(res.columns, row, strict=False))
        out[_knorm(tuple(rec.get(k) for k in key))] = rec
    return out


# ----------------------------- sample pre-check (cheap tripwire before the full compare) -----
@dataclass
class PrecheckResult:
    short_circuit: bool
    sampled: int
    matched: int
    mismatched: int
    match_rate_pct: float
    differences: list  # list[RowDiff]
    src_total: int | None
    dst_total: int | None
    note: str
    common: int = 0  # sampled keys present in BOTH sides (the ones we can value-compare)
    source_only: int = 0  # sampled keys missing from the target (cheap for the full compare)
    inconclusive: bool = False  # couldn't find enough common keys to judge — tell the user


def precheck_short_circuit(common: int, value_mismatched: int, min_common: int,
                           max_mismatch_pct: float) -> bool:
    """Decide whether the sample justifies skipping the full compare. Short-circuit ONLY when
    enough keys exist in BOTH sides (``common >= min_common``) AND their VALUES diverge heavily.

    Two things this deliberately does NOT do:
      * It never trips on source-only (missing) rows — those are cheap for the full hash compare
        to enumerate, so a sample that simply didn't land on common rows must not force a verdict.
      * If we couldn't find enough common keys, we don't trust the sample at all and let the full
        compare run (it has its own fail-fast, so runtime is still bounded)."""
    if common < min_common:
        return False
    return (100.0 * value_mismatched / common) > max_mismatch_pct


def _count(conn: Any, from_sql: str, predicate: str | None) -> int | None:
    where = f" WHERE {predicate}" if predicate else ""
    try:
        res = conn.execute(f"SELECT COUNT(*) AS __C FROM {from_sql}{where}")
        return int(res.rows[0][0]) if res.rows else 0
    except Exception:  # noqa: BLE001
        return None


def _sample_keys(conn: Any, from_sql: str, predicate: str | None, key: list[str], n: int) -> list[tuple]:
    """A cheap, approximately-random fixed-size key sample (Snowflake SAMPLE ... ROWS)."""
    keysel = ", ".join(_q(k) for k in key)
    where = f" WHERE {predicate}" if predicate else ""
    res = conn.execute(f"SELECT {keysel} FROM {from_sql}{where} SAMPLE ({int(n)} ROWS)")
    idx = {c.upper(): i for i, c in enumerate(res.columns)}
    kpos = [idx[k.upper()] for k in key]
    return [tuple(row[p] for p in kpos) for row in res.rows]


def sample_precheck(
    src_conn: Any,
    dst_conn: Any,
    *,
    src_from: str,
    dst_from: str,
    src_predicate: str | None,
    dst_predicate: str | None,
    key: list[str],
    columns: list[str],
    dst_key: list[str] | None = None,
    dst_columns: list[str] | None = None,
    column_types: dict[str, str] | None = None,
    options: CompareOptions | None = None,
    rounds: int = 3,
    sample_size: int = 10,
    max_mismatch_pct: float = 20.0,
    min_common: int | None = None,
    max_rounds: int | None = None,
) -> PrecheckResult:
    """Cheap sampling guard run BEFORE the full hash compare.

    Pull a small random key sample from the source, fetch those rows from both sides, and compare
    them. The verdict is based on the VALUE mismatch rate among keys present in BOTH sides — if
    that's high, the full per-key hash compare would be prohibitively slow (it pulls every key
    across the wire) and "these differ a lot" is already clear, so we short-circuit.

    Crucially, a sampled source key that's MISSING from the target does not count toward the
    mismatch verdict — missing rows are cheap for the full compare to enumerate. If the first
    sample lands mostly on source-only keys (different key ranges, unrepresentative draw), we keep
    sampling — up to ``max_rounds`` — to find enough keys present in both (``min_common``) to judge
    values. If we still can't, we DON'T short-circuit: the full compare runs (bounded by its own
    fail-fast) rather than trusting a sample that never found common ground.
    """
    opts = options or CompareOptions()
    types = column_types or {}
    key = list(key)
    dst_key = list(dst_key) if dst_key else list(key)
    dst_columns = list(dst_columns) if dst_columns else list(columns)
    key_up = {k.upper() for k in key}
    pairs = [(s, d) for s, d in zip(columns, dst_columns, strict=False) if s.upper() not in key_up]
    src_all = list(key) + [s for s, _ in pairs]
    dst_all = list(dst_key) + [d for _, d in pairs]

    want_common = max(1, min_common if min_common is not None else sample_size)
    cap_rounds = max(rounds, max_rounds if max_rounds is not None else rounds * 4)

    matched = value_mismatched = source_only = sampled = common = 0
    differences: list[RowDiff] = []
    seen: set[tuple] = set()

    attempt = 0
    while attempt < cap_rounds:
        attempt += 1
        try:
            raw = _sample_keys(src_conn, src_from, src_predicate, key, sample_size)
        except Exception:  # noqa: BLE001 — sampling unsupported ⇒ let the full compare decide
            break
        raw = [rk for rk in raw if _knorm(rk) not in seen]
        if not raw:
            continue
        for rk in raw:
            seen.add(_knorm(rk))
        src_rows = _fetch_rows_by_key(src_conn, src_from, src_predicate, src_all, key, raw)
        dst_rows = _fetch_rows_by_key(dst_conn, dst_from, dst_predicate, dst_all, dst_key, raw)
        for rk in raw:
            nk = _knorm(rk)
            sr, dr = src_rows.get(nk), dst_rows.get(nk)
            if sr is None:
                continue  # sampled key vanished between queries; skip, don't count
            sampled += 1
            kd = dict(zip(key, rk, strict=False))
            if dr is None:
                # Missing from target — a cheap, structural diff. Recorded, but it does NOT feed
                # the value-divergence verdict; we keep sampling to find keys present in both.
                source_only += 1
                differences.append(RowDiff(kd, _STATUS_OF[B_SOURCE_ONLY], B_SOURCE_ONLY, record=sr))
                continue
            common += 1
            cells = []
            row_differs = False
            for s, d in pairs:
                canon = canonicalize(types.get(s, "string"))
                eq = values_equal(
                    sr.get(s), dr.get(d), canon,
                    ignore_spaces=opts.ignore_spaces,
                    null_equals_empty=opts.null_equals_empty,
                    case_insensitive=opts.case_insensitive,
                    tolerance=opts.tolerance,
                )
                if not eq:
                    row_differs = True
                cells.append(CellDiff(s, sr.get(s), dr.get(d), str(canon), differs=not eq))
            if row_differs:
                value_mismatched += 1
                differences.append(RowDiff(kd, _STATUS_OF[B_VALUE_DIFF], B_VALUE_DIFF, cells=cells))
            else:
                matched += 1
        # Enough common keys to judge values, and we've done the baseline rounds → stop early.
        if common >= want_common and attempt >= rounds:
            break

    mismatched = value_mismatched + source_only  # total differing in the sample (for reporting)
    rate = round(100.0 * matched / common, 2) if common else 100.0
    value_mismatch_pct = (100.0 * value_mismatched / common) if common else 0.0
    short = precheck_short_circuit(common, value_mismatched, want_common, max_mismatch_pct)
    # Inconclusive = we SAMPLED something but couldn't find enough common keys to judge values
    # (the sampled source rows were largely/entirely absent from the target). Not a clean pass —
    # the caller surfaces this so the user knows the sample guard couldn't verify.
    inconclusive = not short and sampled > 0 and common < want_common
    src_total = _count(src_conn, src_from, src_predicate) if short else None
    dst_total = _count(dst_conn, dst_from, dst_predicate) if short else None
    if inconclusive:
        note = (
            f"sampled {sampled} key(s) over {attempt} round(s): only {common} present in both "
            f"(target missing {source_only}) — too few common rows to judge; ran the full compare"
        )
    else:
        note = (
            f"sampled {sampled} key(s): {common} common ({value_mismatched} value-diff, "
            f"{value_mismatch_pct:.0f}%), {source_only} source-only"
        )
    return PrecheckResult(
        short, sampled, matched, mismatched, rate, differences, src_total, dst_total, note,
        common=common, source_only=source_only, inconclusive=inconclusive,
    )


def outcome_from_precheck(pc: PrecheckResult) -> CompareOutcome:
    """A CompareOutcome for a sampled short-circuit — counts are over the SAMPLE, not the full
    table (the caller surfaces a warning saying so)."""
    summary = CompareSummary(
        src_row_count=pc.src_total or 0,
        dst_row_count=pc.dst_total or 0,
        keys_total=pc.sampled,
        matched_rows=pc.matched,
        mismatched_rows=sum(1 for d in pc.differences if d.bucket == B_VALUE_DIFF),
        src_only_rows=sum(1 for d in pc.differences if d.bucket == B_SOURCE_ONLY),
        dst_only_rows=0,
        ambiguous_rows=0,
        match_rate_pct=pc.match_rate_pct,
        per_column_mismatch={},
    )
    return CompareOutcome(summary=summary, differences=pc.differences[:100], passed_sample=[])


def compare_hash(
    src_conn: Any,
    dst_conn: Any,
    *,
    src_from: str,
    dst_from: str,
    src_predicate: str | None,
    dst_predicate: str | None,
    key: list[str],
    columns: list[str],
    dst_key: list[str] | None = None,
    dst_columns: list[str] | None = None,
    column_types: dict[str, str] | None = None,
    options: CompareOptions | None = None,
) -> tuple[CompareOutcome, list[str]]:
    """Pushdown hash compare. `dst_key`/`dst_columns` (parallel to `key`/`columns`) let the
    target use different column names — each side hashes its own names in the same pair
    order, and results are reported under the SOURCE names."""
    opts = options or CompareOptions()
    types = column_types or {}
    key = list(key)
    dst_key = list(dst_key) if dst_key else list(key)
    dst_columns = list(dst_columns) if dst_columns else list(columns)
    key_up = {k.upper() for k in key}
    # keep source/target column pairs aligned, dropping any that is a key column
    pairs = [(s, d) for s, d in zip(columns, dst_columns, strict=False) if s.upper() not in key_up]
    columns = [s for s, _ in pairs]
    dst_columns = [d for _, d in pairs]
    src_all = list(key) + columns
    dst_all = list(dst_key) + dst_columns
    notes: list[str] = []

    src_payload = [_norm_expr(s, canonicalize(types.get(s, "string")), opts) for s in columns]
    dst_payload = [_norm_expr(d, canonicalize(types.get(s, "string")), opts) for s, d in pairs]
    src = _pull_aggregate(src_conn, src_from, src_predicate, key, src_payload)
    dst = _pull_aggregate(dst_conn, dst_from, dst_predicate, dst_key, dst_payload)

    all_keys = set(src) | set(dst)
    matched = mismatched = ambiguous = 0
    src_only_rows = dst_only_rows = 0
    src_total = sum(v[0] for v in src.values())
    dst_total = sum(v[0] for v in dst.values())

    diff_nk: list[tuple] = []  # 1:1 value_difference (normalized keys)
    src_only_nk: list[tuple] = []
    dst_only_nk: list[tuple] = []
    amb_entries: list[tuple] = []  # (nk, scnt, dcnt)

    for k in all_keys:
        s, d = src.get(k), dst.get(k)
        if s and not d:
            src_only_rows += s[0]
            src_only_nk.append(k)
        elif d and not s:
            dst_only_rows += d[0]
            dst_only_nk.append(k)
        else:
            scnt, dcnt = s[0], d[0]
            if scnt == 1 and dcnt == 1:
                if s[1] == d[1]:
                    matched += 1
                else:
                    mismatched += 1
                    diff_nk.append(k)
            else:
                ambiguous += 1
                amb_entries.append((k, scnt, dcnt))

    keys_total = len(all_keys)
    match_rate = round(100.0 * matched / keys_total, 2) if keys_total else 100.0

    def _keydict(nk: tuple) -> dict:
        raw = (src.get(nk) or dst.get(nk))[2]  # report under SOURCE key names
        return dict(zip(key, raw, strict=False))

    # ---- Phase 2: bounded cell-level detail for differing keys ----
    cap = opts.bad_record_limit
    differences: list[RowDiff] = []
    per_column: dict[str, int] = {}

    try:
        vd = diff_nk[:cap]
        src_rows = _fetch_rows_by_key(
            src_conn, src_from, src_predicate, src_all, key, [src[nk][2] for nk in vd]
        )
        dst_rows = _fetch_rows_by_key(
            dst_conn, dst_from, dst_predicate, dst_all, dst_key, [dst[nk][2] for nk in vd]
        )
        for nk in vd:
            sr, dr = src_rows.get(nk, {}), dst_rows.get(nk, {})
            cells = []
            for s, d in pairs:
                canon = canonicalize(types.get(s, "string"))
                equal = values_equal(
                    sr.get(s),
                    dr.get(d),
                    canon,
                    ignore_spaces=opts.ignore_spaces,
                    null_equals_empty=opts.null_equals_empty,
                    case_insensitive=opts.case_insensitive,
                    tolerance=opts.tolerance,
                )
                if not equal:
                    per_column[s] = per_column.get(s, 0) + 1
                cells.append(CellDiff(s, sr.get(s), dr.get(d), str(canon), differs=not equal))
            differences.append(
                RowDiff(_keydict(nk), _STATUS_OF[B_VALUE_DIFF], B_VALUE_DIFF, cells=cells)
            )

        so = _fetch_rows_by_key(
            src_conn,
            src_from,
            src_predicate,
            src_all,
            key,
            [src[nk][2] for nk in src_only_nk[:cap]],
        )
        for nk in src_only_nk[:cap]:
            differences.append(
                RowDiff(
                    _keydict(nk),
                    _STATUS_OF[B_SOURCE_ONLY],
                    B_SOURCE_ONLY,
                    record=so.get(nk, _keydict(nk)),
                )
            )
        do = _fetch_rows_by_key(
            dst_conn,
            dst_from,
            dst_predicate,
            dst_all,
            dst_key,
            [dst[nk][2] for nk in dst_only_nk[:cap]],
        )
        dst_to_src = dict(zip(dst_all, src_all, strict=False))  # target names → source names
        for nk in dst_only_nk[:cap]:
            raw = do.get(nk, {})
            rec = {dst_to_src.get(k2, k2): v for k2, v in raw.items()} if raw else _keydict(nk)
            differences.append(
                RowDiff(_keydict(nk), _STATUS_OF[B_TARGET_ONLY], B_TARGET_ONLY, record=rec)
            )
        if per_column:
            notes.append("per_column_mismatch reflects the sampled differing rows (hash mode)")
    except Exception as exc:  # noqa: BLE001 — degrade to key-only, never fail or move the table
        differences = [
            RowDiff(_keydict(nk), _STATUS_OF[B_VALUE_DIFF], B_VALUE_DIFF) for nk in diff_nk[:cap]
        ]
        differences += [
            RowDiff(_keydict(nk), _STATUS_OF[B_SOURCE_ONLY], B_SOURCE_ONLY)
            for nk in src_only_nk[:cap]
        ]
        differences += [
            RowDiff(_keydict(nk), _STATUS_OF[B_TARGET_ONLY], B_TARGET_ONLY)
            for nk in dst_only_nk[:cap]
        ]
        notes.append(f"column-level detail unavailable in hash mode ({exc}); showing keys only")

    for nk, scnt, dcnt in amb_entries[:cap]:
        differences.append(
            RowDiff(
                _keydict(nk), _STATUS_OF[B_AMBIGUOUS], B_AMBIGUOUS, src_count=scnt, dst_count=dcnt
            )
        )

    summary = CompareSummary(
        src_row_count=src_total,
        dst_row_count=dst_total,
        keys_total=keys_total,
        matched_rows=matched,
        mismatched_rows=mismatched,
        src_only_rows=src_only_rows,
        dst_only_rows=dst_only_rows,
        ambiguous_rows=ambiguous,
        match_rate_pct=match_rate,
        per_column_mismatch=per_column,
    )
    return CompareOutcome(summary=summary, differences=differences, passed_sample=[]), notes
