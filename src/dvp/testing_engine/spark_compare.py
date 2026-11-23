"""Spark-based data compare — the same four buckets as the pure engine, at scale.

Implements datadiff's key-group semantics on Spark DataFrames:
  per-key counts → classify each key (source_only / target_only / 1:1 compare /
  ambiguous) → aggregate bucket counts + per-column mismatches → collect *bounded*
  samples. Only samples are brought to the driver, so this scales to large tables while
  producing byte-for-byte the same summary as `compare_rows` (test-anchored parity).
"""

from __future__ import annotations

from typing import Any

from dvp.testing_engine.rowcompare import (
    _STATUS_OF,
    B_AMBIGUOUS,
    B_MATCH,
    B_SOURCE_ONLY,
    B_TARGET_ONLY,
    B_VALUE_DIFF,
    CellDiff,
    CompareOptions,
    CompareOutcome,
    CompareSummary,
    RowDiff,
)
from dvp.testing_engine.typemap import CanonicalType, canonicalize


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)  # Decimal / datetime / etc. → string for JSON + UI


def _col_equal(a, b, canon: CanonicalType, opts: CompareOptions):  # noqa: ANN001
    from pyspark.sql import functions as F

    if canon == CanonicalType.NUMERIC:
        x, y = a.cast("double"), b.cast("double")
        both_null = x.isNull() & y.isNull()
        return both_null | (
            x.isNotNull() & y.isNotNull() & (F.abs(x - y) <= F.lit(float(opts.tolerance)))
        )

    def norm(c):  # noqa: ANN001
        s = c.cast("string")
        if opts.ignore_spaces:
            s = F.trim(s)
        if opts.case_insensitive:
            s = F.lower(s)
        if opts.null_equals_empty:
            s = F.when(s.isNull() | F.lower(F.trim(s)).isin("", "null"), None).otherwise(s)
        return s

    return norm(a).eqNullSafe(norm(b))


def compare_spark(
    spark,  # noqa: ANN001
    src_df,  # noqa: ANN001
    dst_df,  # noqa: ANN001
    *,
    key: list[str],
    columns: list[str],
    column_types: dict[str, str] | None = None,
    options: CompareOptions | None = None,
) -> CompareOutcome:
    from pyspark.sql import functions as F

    opts = options or CompareOptions()
    types = column_types or {}
    key = list(key)
    columns = [c for c in columns if c not in key]

    src = src_df.select(*[F.col(c) for c in key + columns])
    dst = dst_df.select(*[F.col(c) for c in key + columns])

    # ---- per-key counts → bucket classification ----
    sc = src.groupBy(*key).agg(F.count(F.lit(1)).alias("__scnt"))
    dc = dst.groupBy(*key).agg(F.count(F.lit(1)).alias("__dcnt"))
    stats = sc.join(dc, key, "full_outer").fillna(0, subset=["__scnt", "__dcnt"]).cache()

    keys_total = stats.count()
    source_only_rows = int(
        stats.filter("__dcnt = 0").agg(F.coalesce(F.sum("__scnt"), F.lit(0))).collect()[0][0]
    )
    target_only_rows = int(
        stats.filter("__scnt = 0").agg(F.coalesce(F.sum("__dcnt"), F.lit(0))).collect()[0][0]
    )
    amb_filter = "__scnt >= 1 AND __dcnt >= 1 AND NOT (__scnt = 1 AND __dcnt = 1)"
    ambiguous_rows = stats.filter(amb_filter).count()

    # ---- 1:1 keys → cell comparison ----
    one_keys = stats.filter("__scnt = 1 AND __dcnt = 1").select(*key)
    s1 = src.join(one_keys, key).select(*key, *[F.col(c).alias(f"__s_{c}") for c in columns])
    d1 = dst.join(one_keys, key).select(*key, *[F.col(c).alias(f"__d_{c}") for c in columns])
    joined = s1.join(d1, key)
    for c in columns:
        canon = canonicalize(types.get(c, "string"))
        joined = joined.withColumn(
            f"__eq_{c}", _col_equal(F.col(f"__s_{c}"), F.col(f"__d_{c}"), canon, opts)
        )
    any_diff = F.lit(False)
    for c in columns:
        any_diff = any_diff | (~F.col(f"__eq_{c}"))
    joined = joined.withColumn("__any_diff", any_diff).cache()

    matched_rows = joined.filter("__any_diff = false").count()
    mismatched_rows = joined.filter("__any_diff = true").count()

    per_column: dict[str, int] = {}
    if columns:
        agg = joined.agg(
            *[F.sum((~F.col(f"__eq_{c}")).cast("int")).alias(c) for c in columns]
        ).collect()[0]
        for c in columns:
            if agg[c]:
                per_column[c] = int(agg[c])

    # ---- bounded samples (only these hit the driver) ----
    cap = opts.bad_record_limit
    differences: list[RowDiff] = []
    passed: list[RowDiff] = []

    for row in joined.filter("__any_diff = true").limit(cap).collect():
        key_dict = {k: _jsonable(row[k]) for k in key}
        cells = [
            CellDiff(
                c,
                _jsonable(row[f"__s_{c}"]),
                _jsonable(row[f"__d_{c}"]),
                str(canonicalize(types.get(c, "string"))),
                differs=not row[f"__eq_{c}"],
            )
            for c in columns
        ]
        differences.append(RowDiff(key_dict, _STATUS_OF[B_VALUE_DIFF], B_VALUE_DIFF, cells=cells))

    for row in joined.filter("__any_diff = false").limit(opts.pass_sample_limit).collect():
        key_dict = {k: _jsonable(row[k]) for k in key}
        record = {**key_dict, **{c: _jsonable(row[f"__s_{c}"]) for c in columns}}
        passed.append(RowDiff(key_dict, "match", B_MATCH, record=record))

    for row in src.join(stats.filter("__dcnt = 0").select(*key), key).limit(cap).collect():
        differences.append(
            RowDiff(
                {k: _jsonable(row[k]) for k in key},
                _STATUS_OF[B_SOURCE_ONLY],
                B_SOURCE_ONLY,
                record={c: _jsonable(row[c]) for c in key + columns},
            )
        )
    for row in dst.join(stats.filter("__scnt = 0").select(*key), key).limit(cap).collect():
        differences.append(
            RowDiff(
                {k: _jsonable(row[k]) for k in key},
                _STATUS_OF[B_TARGET_ONLY],
                B_TARGET_ONLY,
                record={c: _jsonable(row[c]) for c in key + columns},
            )
        )
    for row in stats.filter(amb_filter).limit(cap).collect():
        differences.append(
            RowDiff(
                {k: _jsonable(row[k]) for k in key},
                _STATUS_OF[B_AMBIGUOUS],
                B_AMBIGUOUS,
                src_count=int(row["__scnt"]),
                dst_count=int(row["__dcnt"]),
            )
        )

    src_total, dst_total = src.count(), dst.count()
    match_rate = round(100.0 * matched_rows / keys_total, 2) if keys_total else 100.0
    summary = CompareSummary(
        src_row_count=src_total,
        dst_row_count=dst_total,
        keys_total=keys_total,
        matched_rows=matched_rows,
        mismatched_rows=mismatched_rows,
        src_only_rows=source_only_rows,
        dst_only_rows=target_only_rows,
        ambiguous_rows=ambiguous_rows,
        match_rate_pct=match_rate,
        per_column_mismatch=per_column,
    )
    stats.unpersist()
    joined.unpersist()
    return CompareOutcome(summary=summary, differences=differences, passed_sample=passed)
