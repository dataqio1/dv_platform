"""Dialect-aware distinct-count SQL — the single place that decides *how* to count distinct
values per engine, so the platform uses a cheap approximation where the warehouse supports it
and an exact count everywhere else.

Approximate distinct (HyperLogLog) is dramatically cheaper than an exact COUNT(DISTINCT) on a
large, high-cardinality column, so we prefer it for profiling/monitoring where a ~2% error is
fine. It must NEVER be used to *prove* a uniqueness constraint (a key check): approximation
can't tell "0 duplicates" from "a few", so those stay exact in the evaluator.

Every result derived from an approximation must be labelled as such in the UI (the profiler
emits a `distinct_approx` flag that flows all the way to the chart + table).
"""

from __future__ import annotations

# Engines with a native approximate-distinct function. Postgres is excluded (needs the hll
# extension); the embedded SQLite used for files/local has none.
APPROX_DISTINCT_DIALECTS = frozenset({"snowflake", "oracle", "bigquery", "redshift", "duckdb"})


def supports_approx_distinct(dialect: str | None) -> bool:
    return (dialect or "").lower() in APPROX_DISTINCT_DIALECTS


def count_distinct_sql(dialect: str | None, col_sql: str, *, approx: bool) -> str:
    """Return the distinct-count expression for `col_sql` (already quoted) on this dialect.

    When `approx` is True and the dialect supports it, use the engine's HLL function with its
    correct syntax (Redshift spells it differently); otherwise fall back to exact COUNT(DISTINCT).
    """
    d = (dialect or "").lower()
    if approx and d in APPROX_DISTINCT_DIALECTS:
        if d == "redshift":
            # Redshift: APPROXIMATE COUNT(DISTINCT col)
            return f"APPROXIMATE COUNT(DISTINCT {col_sql})"
        # Snowflake / Oracle / BigQuery / DuckDB all use APPROX_COUNT_DISTINCT(col)
        return f"APPROX_COUNT_DISTINCT({col_sql})"
    return f"COUNT(DISTINCT {col_sql})"
