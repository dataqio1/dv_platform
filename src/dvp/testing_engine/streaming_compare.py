"""Streaming key-range compare — the cross-database (e.g. Oracle↔Snowflake) correctness floor.

Pushdown engines (recursive hash, Snowflake hash) only work when both sides are the same
engine. Across different engines there's no shared hash, so we compare IN OUR PROCESS — but
loading two whole tables into RAM has a hard size ceiling. This engine removes the ceiling by
walking the key space in bounded windows:

  1. pull the next N source rows ordered by key (``WHERE key > :last ORDER BY key`` + row cap),
  2. pull the target rows in that same key interval (``key > :last AND key <= :hi``),
  3. compare the window with the proven in-memory ``compare_rows`` and accumulate,
  4. advance ``last = hi`` and repeat; a final sweep catches target-only keys past the last
     source key.

Memory stays flat regardless of table size, it works for ANY dialect pair and ANY column type
(no cross-engine normalization needed — the comparison is the same one the in-memory engine uses),
and it reports progress per window so a long run shows how far along it is. v1 requires a single
numeric key so the key-range bounds are exact and collation-independent.
"""

from __future__ import annotations

from collections.abc import Callable

from dvp.testing_engine.cancel import CompareCancelled
from dvp.testing_engine.rowcompare import (
    CompareOptions,
    CompareOutcome,
    CompareSummary,
    RowDiff,
    compare_rows,
)
from dvp.testing_engine.typemap import CanonicalType, canonicalize


def supported(source_key: list[str], key_canonical_numeric: bool) -> bool:
    """v1: a single numeric key column — so ``key > lo AND key <= hi`` partitions the space exactly
    and identically on both engines (no reliance on cross-engine string collation)."""
    return len(source_key) == 1 and key_canonical_numeric


def is_numeric_key(col_types: dict[str, str], key_col: str) -> bool:
    return canonicalize(col_types.get(key_col, "string")) == CanonicalType.NUMERIC


def _q(col: str) -> str:
    return '"' + col.replace('"', '""') + '"'


def _lit(v: object) -> str:
    """A numeric key value as a SQL literal (int/float/Decimal → plain number text)."""
    return str(v)


def _and(a: str | None, b: str | None) -> str | None:
    if a and b:
        return f"({a}) AND {b}"
    return a or b


class _Merged:
    """Accumulates per-window CompareOutcomes into one, enforcing GLOBAL sample caps (so a
    thousand windows don't each contribute their own capped sample)."""

    def __init__(self, opts: CompareOptions) -> None:
        self.opts = opts
        self.src_rows = self.dst_rows = self.keys_total = 0
        self.matched = self.mismatched = self.src_only = self.dst_only = self.ambiguous = 0
        self.per_column: dict[str, int] = {}
        self.bad: list[RowDiff] = []
        self.passed: list[RowDiff] = []
        self._in_bucket: dict[str, int] = {}
        self.stopped_early = False

    def add(self, outcome: CompareOutcome) -> None:
        s = outcome.summary
        self.src_rows += s.src_row_count
        self.dst_rows += s.dst_row_count
        self.keys_total += s.keys_total
        self.matched += s.matched_rows
        self.mismatched += s.mismatched_rows
        self.src_only += s.src_only_rows
        self.dst_only += s.dst_only_rows
        self.ambiguous += s.ambiguous_rows
        for col, n in s.per_column_mismatch.items():
            self.per_column[col] = self.per_column.get(col, 0) + n
        cap = self.opts.bad_record_limit
        for rd in outcome.differences:
            if self._in_bucket.get(rd.bucket, 0) < cap:
                self.bad.append(rd)
                self._in_bucket[rd.bucket] = self._in_bucket.get(rd.bucket, 0) + 1
        for rd in outcome.passed_sample:
            if len(self.passed) < self.opts.pass_sample_limit:
                self.passed.append(rd)

    def total_diff(self) -> int:
        return self.mismatched + self.src_only + self.dst_only

    def finalize(self) -> CompareOutcome:
        match_rate = round(100.0 * self.matched / self.keys_total, 2) if self.keys_total else 100.0
        summary = CompareSummary(
            src_row_count=self.src_rows,
            dst_row_count=self.dst_rows,
            keys_total=self.keys_total,
            matched_rows=self.matched,
            mismatched_rows=self.mismatched,
            src_only_rows=self.src_only,
            dst_only_rows=self.dst_only,
            ambiguous_rows=self.ambiguous,
            match_rate_pct=match_rate,
            per_column_mismatch=self.per_column,
            stopped_early=self.stopped_early,
        )
        return CompareOutcome(summary=summary, differences=self.bad, passed_sample=self.passed)


def compare_streaming(
    *,
    pull: Callable,
    src_conn: object,
    dst_conn: object,
    src_from: str,
    dst_from: str,
    src_predicate: str | None,
    dst_predicate: str | None,
    key: str,
    dst_key: str,
    src_cols: list[str],
    dst_cols: list[str],
    columns: list[str],
    column_mappings: dict | None,
    column_types: dict[str, str],
    dst_column_types: dict[str, str],
    options: CompareOptions,
    window: int,
    total_rows: int = 0,
    log: Callable[[str], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[CompareOutcome, list[str]]:
    """Compare two tables by walking the (single numeric) key space in windows of ``window`` rows.

    ``pull(connector, from_sql, cols, predicate=None, limit=None, order_by=None) -> list[dict]`` is
    the caller's row reader. ``progress(processed_rows, total_rows)`` is invoked after each window.
    Honours fail-fast (stop once ``max_diff_rows`` differing rows are confirmed), cancellation, and
    the wall-clock deadline between windows.
    """
    import time

    _log = log or (lambda *_a, **_k: None)
    qk_s, qk_d = _q(key), _q(dst_key)
    rename = dict(zip(dst_cols, src_cols, strict=False))

    def _rename(rows: list[dict]) -> list[dict]:
        if not column_mappings:
            return rows
        return [{rename.get(k2, k2): v for k2, v in r.items()} for r in rows]

    def _cancelled() -> bool:
        cb = options.should_cancel
        return bool(callable(cb) and cb())

    merged = _Merged(options)
    notes: list[str] = []
    last: object | None = None  # exclusive lower bound on the key; None = from the start
    processed = 0
    windows = 0

    while True:
        if _cancelled():
            raise CompareCancelled()
        if options.deadline_epoch and time.time() >= options.deadline_epoch:
            merged.stopped_early = True
            notes.append("stopped at the time budget — results so far are a lower bound")
            break

        lo_s = f"{qk_s} > {_lit(last)}" if last is not None else None
        src_batch = pull(
            src_conn, src_from, src_cols, _and(src_predicate, lo_s),
            limit=window, order_by=f"{qk_s} ASC",
        )
        raw_len = len(src_batch)
        if raw_len == 0:
            break
        hi = max(r.get(key) for r in src_batch)
        # If the window boundary fell in the middle of a duplicated key, pull the rest of that key
        # so a single key's rows are never split across two windows (keeps the ambiguous bucket right).
        if raw_len >= window:
            eq = pull(src_conn, src_from, src_cols, _and(src_predicate, f"{qk_s} = {_lit(hi)}"))
            if len(eq) > sum(1 for r in src_batch if r.get(key) == hi):
                src_batch = [r for r in src_batch if r.get(key) != hi] + eq

        dst_batch = pull(
            dst_conn, dst_from, dst_cols,
            _and(_and(dst_predicate, f"{qk_d} > {_lit(last)}" if last is not None else None),
                 f"{qk_d} <= {_lit(hi)}"),
        )
        out = compare_rows(
            src_batch, _rename(dst_batch),
            key=[key], columns=columns,
            column_types=column_types, dst_column_types=dst_column_types,
            options=options,
        )
        merged.add(out)
        processed += len(src_batch)
        windows += 1
        last = hi
        if progress is not None:
            progress(processed, total_rows)
        pct = f" ({min(99, int(100 * processed / total_rows))}%)" if total_rows else ""
        _log(f"Streaming window {windows}: {processed:,} source rows compared{pct}")

        if options.fail_fast and merged.total_diff() >= options.max_diff_rows:
            merged.stopped_early = True
            notes.append(
                f"stopped after confirming {merged.total_diff():,} differing rows (fail-fast) — "
                f"counts are a lower bound"
            )
            break
        if raw_len < window:
            break  # the ordered window wasn't full → source is exhausted

    # Target-only tail: keys greater than the last source key we saw (or all target rows if the
    # source was empty). Skipped when we stopped early — the picture is already a lower bound.
    if not merged.stopped_early and not _cancelled():
        tail_pred = dst_predicate if last is None else _and(dst_predicate, f"{qk_d} > {_lit(last)}")
        dst_tail = pull(dst_conn, dst_from, dst_cols, tail_pred)
        if dst_tail:
            out = compare_rows(
                [], _rename(dst_tail),
                key=[key], columns=columns,
                column_types=column_types, dst_column_types=dst_column_types,
                options=options,
            )
            merged.add(out)
    if progress is not None and total_rows:
        progress(max(processed, total_rows), total_rows)  # finish at 100%
    _log(f"Streaming compare finished — {windows} window(s), {processed:,} source rows")
    return merged.finalize(), notes
