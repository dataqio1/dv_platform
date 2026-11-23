"""Cross-type cell comparison — numeric/string/date/timestamp/boolean, mixed types across the
two sides, precision/scale, timezone, null-vs-empty, case, spaces. Pure comparator (no DB)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from dvp.testing_engine.typemap import (
    CanonicalType,
    canonicalize,
    resolve_compare_type,
    values_equal,
)


def eq(src, dst, canonical, **kw):
    return values_equal(src, dst, canonical, **kw)


# ---------------- resolve_compare_type (mixed-type policy) ----------------
def test_resolve_picks_structured_type():
    N, S, D, T, B, U = (
        CanonicalType.NUMERIC, CanonicalType.STRING, CanonicalType.DATE,
        CanonicalType.TIMESTAMP, CanonicalType.BOOLEAN, CanonicalType.UNKNOWN,
    )
    assert resolve_compare_type(N, N) == N
    assert resolve_compare_type(N, S) == N        # NUMBER vs VARCHAR → numeric
    assert resolve_compare_type(S, N) == N        # symmetric
    assert resolve_compare_type(D, T) == T        # DATE vs TIMESTAMP → timestamp (date-aware)
    assert resolve_compare_type(T, S) == T        # timestamp vs string → timestamp
    assert resolve_compare_type(B, S) == B        # boolean vs string → boolean
    assert resolve_compare_type(U, S) == S        # unknown vs string → string
    assert resolve_compare_type(N, T) == S        # incompatible → string (honest mismatch)


# ---------------- numeric family + precision/scale + cross-type ----------------
def test_numeric_precision_and_scale():
    assert eq(123, 123.0, CanonicalType.NUMERIC)           # int vs float
    assert eq("123.00", 123, CanonicalType.NUMERIC)        # VARCHAR "123.00" vs NUMBER
    assert eq("0012300", 12300, CanonicalType.NUMERIC)     # leading zeros
    assert not eq(123.45, 123.50, CanonicalType.NUMERIC)   # genuine difference
    assert eq(100.00, 100.001, CanonicalType.NUMERIC, tolerance=0.01)   # within tolerance
    assert not eq(100.00, 100.1, CanonicalType.NUMERIC, tolerance=0.01)


def test_numeric_vs_nonnumeric_string_falls_back_honestly():
    # NUMBER vs VARCHAR "abc": can't both be numbers → string compare → differ (not a false match)
    assert not eq(123, "abc", CanonicalType.NUMERIC)


# ---------------- string: spaces, case, null-vs-empty ----------------
def test_string_spaces_case_null_empty():
    assert eq(" hello ", "hello", CanonicalType.STRING)                  # ignore_spaces default
    assert eq("ABC", "abc", CanonicalType.STRING, case_insensitive=True)
    assert not eq("ABC", "abc", CanonicalType.STRING)                    # case-sensitive default
    assert eq(None, "", CanonicalType.STRING)                           # null_equals_empty default
    assert eq("", None, CanonicalType.STRING)                           # symmetric
    assert not eq(None, "x", CanonicalType.STRING)
    assert eq("007", "007", CanonicalType.STRING)                       # codes stay exact
    assert not eq("007", "7", CanonicalType.STRING)                     # string ≠ numeric coercion


# ---------------- date vs timestamp, timezone, fractional seconds ----------------
def test_date_vs_timestamp_compares_date_part():
    assert eq("2026-09-18", "2026-09-18 00:00:00", CanonicalType.DATE)
    assert eq(date(2026, 9, 18), datetime(2026, 9, 18, 0, 0), CanonicalType.TIMESTAMP)
    assert not eq("2026-09-18", "2026-09-19 00:00:00", CanonicalType.DATE)


def test_timestamp_timezone_normalized_to_utc():
    # 10:45 UTC == 12:45 +02:00 (same instant)
    assert eq("2026-09-18T10:45:00+00:00", "2026-09-18T12:45:00+02:00", CanonicalType.TIMESTAMP)
    assert eq("2026-09-18T10:45:00Z", "2026-09-18 10:45:00", CanonicalType.TIMESTAMP)  # naive=UTC


def test_timestamp_fractional_tolerance():
    a = "2026-09-18T10:45:00.000000"
    b = "2026-09-18T10:45:00.400000"
    assert eq(a, b, CanonicalType.TIMESTAMP, tolerance=1.0)     # within 1s
    assert not eq(a, b, CanonicalType.TIMESTAMP)                # exact → differ
    assert not eq(a, "2026-09-18T10:46:00", CanonicalType.TIMESTAMP, tolerance=1.0)


def test_unparseable_temporal_falls_back_to_string():
    assert eq("not-a-date", "not-a-date", CanonicalType.TIMESTAMP)
    assert not eq("not-a-date", "2026-09-18", CanonicalType.TIMESTAMP)


# ---------------- boolean variants across representations ----------------
def test_boolean_variants():
    for t in (True, 1, "1", "true", "T", "yes", "Y"):
        for f in (False, 0, "0", "false", "N", "no"):
            assert eq(t, t, CanonicalType.BOOLEAN)
            assert not eq(t, f, CanonicalType.BOOLEAN)
    assert eq("Y", 1, CanonicalType.BOOLEAN)        # CHAR(1) 'Y' vs NUMBER(1) 1
    assert eq("true", True, CanonicalType.BOOLEAN)
    # non-boolean-ish value on one side → honest string compare, not a mis-coerce
    assert not eq("maybe", "1", CanonicalType.BOOLEAN)


# ---------------- end-to-end via compare_rows with mixed source/target types ----------------
def test_compare_rows_mixed_types_match():
    from dvp.testing_engine.rowcompare import CompareOptions, compare_rows

    # source: NUMBER amount, DATE d  |  target: VARCHAR amount "100.00", TIMESTAMP d
    src = [{"ID": 1, "AMT": 100, "D": "2026-09-18"}]
    dst = [{"ID": 1, "AMT": "100.00", "D": "2026-09-18 00:00:00"}]
    out = compare_rows(
        src, dst, key=["ID"], columns=["AMT", "D"],
        column_types={"ID": "NUMBER", "AMT": "NUMBER", "D": "DATE"},
        dst_column_types={"AMT": "VARCHAR", "D": "TIMESTAMP"},
        options=CompareOptions(),
    )
    assert out.summary.matched_rows == 1
    assert out.summary.mismatched_rows == 0


def test_compare_rows_mixed_types_real_difference():
    from dvp.testing_engine.rowcompare import compare_rows

    src = [{"ID": 1, "AMT": 100}]
    dst = [{"ID": 1, "AMT": "100.50"}]  # genuinely different value
    out = compare_rows(
        src, dst, key=["ID"], columns=["AMT"],
        column_types={"ID": "NUMBER", "AMT": "NUMBER"},
        dst_column_types={"AMT": "VARCHAR"},
    )
    assert out.summary.mismatched_rows == 1


def test_canonicalize_covers_common_warehouse_types():
    assert canonicalize("NUMBER(38,0)") == CanonicalType.NUMERIC
    assert canonicalize("VARCHAR2(100)") == CanonicalType.STRING
    assert canonicalize("TIMESTAMP_NTZ(9)") == CanonicalType.TIMESTAMP
    assert canonicalize("DATE") == CanonicalType.DATE
    assert canonicalize("BOOLEAN") == CanonicalType.BOOLEAN
    assert canonicalize("JSONB") == CanonicalType.UNKNOWN


def test_naive_utc_assumption_is_consistent():
    # a naive datetime object and its ISO string should compare equal under TIMESTAMP
    dt = datetime(2026, 9, 18, 10, 45, tzinfo=UTC)
    assert eq(dt, "2026-09-18T10:45:00Z", CanonicalType.TIMESTAMP)
