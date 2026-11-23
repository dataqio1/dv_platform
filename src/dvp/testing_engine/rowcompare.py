"""Pure key-based cell-level row comparison with four result buckets.

Mirrors the proven datadiff categorisation (`DataCompareUtil`): group both sides by key,
then classify each key into one of four buckets —

  * **value_difference** (bucket 1): exactly one row per key on *both* sides and one or
    more cells differ (datadiff's `srcTargetSingleKeyMisMatchCount`).
  * **source_only**      (bucket 2): key present only in the source.
  * **target_only**      (bucket 3): key present only in the target.
  * **ambiguous**        (bucket 4): multiple rows share the key on either side
    (many-to-many) — set aside for now (counted, not diffed).

Keys that are 1:1 and fully equal are **matched** (pass). We keep a capped sample of
passing records *and* a capped sample of each bad bucket, so the UI can show both good
and bad examples. The PySpark path implements the same semantics at scale; this pure
version is unit-testable without a cluster and runs directly on pulled rows.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from dvp.testing_engine.typemap import (
    CanonicalType,
    canonicalize,
    resolve_compare_type,
    values_equal,
)

# Bucket names (stable, used by the API/UI).
B_MATCH = "match"
B_VALUE_DIFF = "value_difference"
B_SOURCE_ONLY = "source_only"
B_TARGET_ONLY = "target_only"
B_AMBIGUOUS = "ambiguous"

# Internal RowDiff.status values kept backward-compatible with earlier callers/tests.
_STATUS_OF = {
    B_VALUE_DIFF: "mismatch",
    B_SOURCE_ONLY: "src_only",
    B_TARGET_ONLY: "dst_only",
    B_AMBIGUOUS: "ambiguous",
    B_MATCH: "match",
}


@dataclass
class CompareOptions:
    ignore_spaces: bool = True
    null_equals_empty: bool = True
    case_insensitive: bool = False
    tolerance: float = 0.0
    compare_time: bool = True  # compare the time of day on timestamp columns (False = date only)
    bad_record_limit: int = 100  # per bucket
    pass_sample_limit: int = 50
    # --- Fail-fast / budget guards for pushdown engines on huge warehouse tables ---
    # Stop as soon as it's clear the tables differ substantially, and never let a single run
    # issue unbounded queries or run past a wall-clock deadline (so a compare can't run for
    # hours or keep spawning Snowflake queries long after it should have given up).
    fail_fast: bool = True                # abort once >= max_diff_rows differing rows are confirmed (default ON)
    max_diff_rows: int = 1000             # the fail-fast tripwire
    max_queries: int = 2000               # hard cap on pushdown queries per compare (0 = unlimited)
    deadline_epoch: float | None = None   # wall-clock stop (time.time() >= this → abort)
    should_cancel: object = None          # callable()->bool; polled between queries for user cancel
    memory_gb: float | None = None        # RAM budget (GB) for the in-memory/Spark path (OOM guard)


@dataclass
class CellDiff:
    column: str
    src: object
    dst: object
    canonical_type: str
    differs: bool = True  # False for matching columns (shown for full-row context)
    compared: bool = True  # False for "extra" columns captured for context but NOT part of the compare


@dataclass
class RowDiff:
    key: dict
    status: str  # match | mismatch | src_only | dst_only | ambiguous
    bucket: str = ""
    cells: list[CellDiff] = field(default_factory=list)  # value_difference
    record: dict | None = None  # match / source_only / target_only (the row itself)
    src_count: int = 1
    dst_count: int = 1


@dataclass
class CompareSummary:
    src_row_count: int
    dst_row_count: int
    keys_total: int
    matched_rows: int
    mismatched_rows: int  # bucket 1 (value_difference)
    src_only_rows: int  # bucket 2
    dst_only_rows: int  # bucket 3
    ambiguous_rows: int  # bucket 4
    match_rate_pct: float
    per_column_mismatch: dict[str, int]
    # True when fail-fast tripped and the engine stopped before scanning everything.
    # keys_total/matched/diff counts are then a lower bound, not the full picture.
    stopped_early: bool = False
    # Non-compared ("extra") columns captured for results context, when the user opted in.
    extra_columns: list[str] = field(default_factory=list)


@dataclass
class CompareOutcome:
    summary: CompareSummary
    differences: list[RowDiff]  # bad buckets (capped per bucket)
    passed_sample: list[RowDiff] = field(default_factory=list)


def _key_of(row: dict, key: list[str]) -> tuple:
    return tuple(row.get(k) for k in key)


def _project(row: dict, key: list[str], columns: list[str], extras: list[str] | None = None) -> dict:
    cols = list(key) + [c for c in columns if c not in key]
    if extras:
        taken = set(cols)
        cols += [c for c in extras if c not in taken]
    return {c: row.get(c) for c in cols}


def compare_rows(
    src_rows: list[dict],
    dst_rows: list[dict],
    *,
    key: list[str],
    columns: list[str],
    column_types: dict[str, str] | None = None,
    dst_column_types: dict[str, str] | None = None,
    options: CompareOptions | None = None,
    extra_columns: list[str] | None = None,
) -> CompareOutcome:
    opts = options or CompareOptions()
    types = column_types or {}
    # Non-compared columns captured for context (shown in results, flagged not-compared). Never
    # feed the match verdict — they only ride along in records/cells for the user to see.
    extras = list(extra_columns or [])
    # Target-side types keyed by the SAME (source) column name, so a column whose type differs
    # across sources (NUMBER vs VARCHAR, DATE vs TIMESTAMP) compares by the right rule.
    dst_types = dst_column_types or {}

    src_groups: dict[tuple, list[dict]] = defaultdict(list)
    dst_groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in src_rows:
        src_groups[_key_of(r, key)].append(r)
    for r in dst_rows:
        dst_groups[_key_of(r, key)].append(r)

    matched = mismatched = src_only = dst_only = ambiguous = 0
    per_column: dict[str, int] = {}
    bad: list[RowDiff] = []
    passed: list[RowDiff] = []
    cap = opts.bad_record_limit
    in_bucket: dict[str, int] = defaultdict(int)

    def add_bad(rd: RowDiff) -> None:
        if in_bucket[rd.bucket] < cap:
            bad.append(rd)
            in_bucket[rd.bucket] += 1

    for k in set(src_groups) | set(dst_groups):
        srows = src_groups.get(k, [])
        drows = dst_groups.get(k, [])
        key_dict = dict(zip(key, k, strict=True))

        if srows and not drows:  # bucket 2
            src_only += len(srows)
            for r in srows:
                add_bad(
                    RowDiff(
                        key_dict,
                        _STATUS_OF[B_SOURCE_ONLY],
                        B_SOURCE_ONLY,
                        record=_project(r, key, columns, extras),
                    )
                )
        elif drows and not srows:  # bucket 3
            dst_only += len(drows)
            for r in drows:
                add_bad(
                    RowDiff(
                        key_dict,
                        _STATUS_OF[B_TARGET_ONLY],
                        B_TARGET_ONLY,
                        record=_project(r, key, columns, extras),
                    )
                )
        elif len(srows) == 1 and len(drows) == 1:  # bucket 1 or match
            sr, dr = srows[0], drows[0]
            # Build a cell for EVERY compared column (matching + differing) so the UI can
            # show the full record with the differences highlighted — not just the diffs.
            cells: list[CellDiff] = []
            any_diff = False
            for col in columns:
                src_canon = canonicalize(types.get(col, "string"))
                dst_canon = canonicalize(dst_types.get(col, types.get(col, "string")))
                canonical = resolve_compare_type(src_canon, dst_canon)
                equal = values_equal(
                    sr.get(col),
                    dr.get(col),
                    canonical,
                    ignore_spaces=opts.ignore_spaces,
                    null_equals_empty=opts.null_equals_empty,
                    case_insensitive=opts.case_insensitive,
                    tolerance=opts.tolerance,
                    compare_time=opts.compare_time,
                )
                if not equal:
                    any_diff = True
                    per_column[col] = per_column.get(col, 0) + 1
                cells.append(
                    CellDiff(col, sr.get(col), dr.get(col), str(canonical), differs=not equal)
                )
            # Extra (non-compared) columns: ride along as cells flagged compared=False so the UI
            # can show them for context without them ever affecting the match verdict.
            for col in extras:
                cells.append(
                    CellDiff(col, sr.get(col), dr.get(col), "", differs=False, compared=False)
                )
            if any_diff:
                mismatched += 1
                add_bad(RowDiff(key_dict, _STATUS_OF[B_VALUE_DIFF], B_VALUE_DIFF, cells=cells))
            else:
                matched += 1
                if len(passed) < opts.pass_sample_limit:
                    passed.append(
                        RowDiff(key_dict, "match", B_MATCH, record=_project(sr, key, columns, extras))
                    )
        else:  # bucket 4 — many-to-many, set aside for now
            ambiguous += 1
            add_bad(
                RowDiff(
                    key_dict,
                    _STATUS_OF[B_AMBIGUOUS],
                    B_AMBIGUOUS,
                    src_count=len(srows),
                    dst_count=len(drows),
                )
            )

    keys_total = len(set(src_groups) | set(dst_groups))
    match_rate = round(100.0 * matched / keys_total, 2) if keys_total else 100.0
    summary = CompareSummary(
        src_row_count=len(src_rows),
        dst_row_count=len(dst_rows),
        keys_total=keys_total,
        matched_rows=matched,
        mismatched_rows=mismatched,
        src_only_rows=src_only,
        dst_only_rows=dst_only,
        ambiguous_rows=ambiguous,
        match_rate_pct=match_rate,
        per_column_mismatch=per_column,
        extra_columns=extras,
    )
    return CompareOutcome(summary=summary, differences=bad, passed_sample=passed)


__all__ = [
    "CompareOptions",
    "CompareOutcome",
    "CompareSummary",
    "RowDiff",
    "compare_rows",
    "CanonicalType",
]
