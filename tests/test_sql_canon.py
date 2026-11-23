"""Shared SQL numeric canonical — the form the pushdown hash engines use. Executed against real
SQLite so precision + trailing-zero normalization are proven, not assumed."""

from __future__ import annotations

import sqlite3

from dvp.testing_engine.sql_canon import (
    boolean_text,
    numeric_text,
    string_text,
    temporal_text,
)


def _canon(value):  # noqa: ANN001
    conn = sqlite3.connect(":memory:")
    expr = numeric_text("sqlite", "x")
    return conn.execute(f"SELECT {expr} FROM (SELECT ? AS x)", (value,)).fetchone()[0]  # noqa: S608


def _eval(expr, value):  # noqa: ANN001
    conn = sqlite3.connect(":memory:")
    return conn.execute(f"SELECT {expr} FROM (SELECT ? AS x)", (value,)).fetchone()[0]  # noqa: S608


def test_distinct_large_ids_do_not_collapse():
    # The old CAST(... AS DOUBLE) collapsed 18-digit ids to the same float → false match.
    assert _canon(123456789012345678) != _canon(123456789012345679)
    assert _canon(123456789012345678) == "123456789012345678"


def test_trailing_zeros_normalized_integers_untouched():
    assert _canon("1.50") == "1.5"          # trailing zero after the point stripped
    assert _canon("1.5") == "1.5"
    assert _canon("1.0") == "1"
    assert _canon("100.00") == "100"
    assert _canon(100) == "100"             # integer: RTRIM('0') must NOT turn 100 into 1
    assert _canon(0) == "0"


def test_warehouse_forms_preserve_precision():
    # The Oracle/Snowflake forms keep NUMBER(38,10) text — never a float/DOUBLE round-trip.
    for dialect in ("snowflake", "oracle"):
        sql = numeric_text(dialect, '"AMT"').upper()
        assert "DOUBLE" not in sql and "AS REAL" not in sql
        assert "NUMBER(38,10)" in sql


def test_string_text_trim_lower_and_empty_as_null_sqlite():
    expr = string_text("sqlite", "x")  # defaults: ignore_spaces, null_equals_empty
    assert _eval(expr, "  hi  ") == "hi"              # trailing/leading spaces ignored
    assert _eval(expr, "") is None                    # '' collapses to NULL (Oracle parity)
    ci = string_text("sqlite", "x", case_insensitive=True)
    assert _eval(ci, "ABc") == "abc"
    keep = string_text("sqlite", "x", ignore_spaces=False, null_equals_empty=False)
    assert _eval(keep, "  hi ") == "  hi "            # untouched when options off


def test_boolean_text_sqlite_passthrough_consistent():
    # sqlite path is a plain text cast (same-engine both sides agree); 1/0 render stably.
    expr = boolean_text("sqlite", "x")
    assert _eval(expr, 1) == "1"
    assert _eval(expr, 0) == "0"


def test_boolean_text_warehouse_maps_truthy_variants():
    for dialect in ("oracle", "snowflake"):
        sql = boolean_text(dialect, '"FLAG"')
        assert "'true'" in sql and "'false'" in sql
        assert "IS NULL THEN NULL" in sql
    # Oracle treats numeric 1 and Y/T flags as true; Snowflake normalizes via TO_VARCHAR.
    assert "'Y'" in boolean_text("oracle", '"FLAG"')
    assert "TO_VARCHAR" in boolean_text("snowflake", '"FLAG"')


def test_temporal_text_timestamp_has_microseconds_date_does_not():
    # TIMESTAMP keeps sub-second precision (FF6) so Snowflake(9) vs Oracle(6) sub-second diffs
    # aren't truncated to whole seconds and falsely pruned as equal; DATE stays calendar-only.
    for dialect in ("oracle", "snowflake"):
        ts = temporal_text(dialect, '"TS"', is_date=False)
        dt = temporal_text(dialect, '"D"', is_date=True)
        assert "FF6" in ts
        assert "FF6" not in dt and "YYYY-MM-DD" in dt
