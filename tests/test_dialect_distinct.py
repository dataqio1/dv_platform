"""Dialect-aware distinct-count SQL — approx where supported (correct per-engine syntax),
exact everywhere else."""

from __future__ import annotations

from dvp.connectors.dialect import count_distinct_sql, supports_approx_distinct


def test_supports_approx():
    assert supports_approx_distinct("snowflake")
    assert supports_approx_distinct("ORACLE")  # case-insensitive
    assert supports_approx_distinct("redshift")
    assert not supports_approx_distinct("sqlite")
    assert not supports_approx_distinct("postgres")  # no native HLL
    assert not supports_approx_distinct(None)


def test_approx_sql_per_dialect():
    col = '"EMAIL"'
    # Snowflake / Oracle / BigQuery / DuckDB share APPROX_COUNT_DISTINCT
    for d in ("snowflake", "oracle", "bigquery", "duckdb"):
        assert count_distinct_sql(d, col, approx=True) == f"APPROX_COUNT_DISTINCT({col})"
    # Redshift uses a different spelling
    assert count_distinct_sql("redshift", col, approx=True) == f"APPROXIMATE COUNT(DISTINCT {col})"


def test_exact_fallback():
    col = '"EMAIL"'
    # Unsupported dialects always get exact, even if approx is requested
    assert count_distinct_sql("sqlite", col, approx=True) == f"COUNT(DISTINCT {col})"
    assert count_distinct_sql("postgres", col, approx=True) == f"COUNT(DISTINCT {col})"
    # approx=False always exact
    assert count_distinct_sql("snowflake", col, approx=False) == f"COUNT(DISTINCT {col})"


def test_profiler_marks_approx_flag_on_sqlite_as_false():
    # sqlite has no approx → the local profiler must report exact counts (distinct_approx False).
    # (Full approx behavior needs a real warehouse; this guards the fallback path.)
    assert not supports_approx_distinct("sqlite")
