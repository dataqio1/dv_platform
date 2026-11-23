from datetime import UTC, date, datetime

from dvp.testing_engine.typemap import (
    CanonicalType,
    canonicalize,
    resolve_compare_type,
    types_match,
    values_equal,
)


def test_canonicalize_cross_dialect():
    assert canonicalize("NUMBER(38,0)") == CanonicalType.NUMERIC
    assert canonicalize("VARCHAR2(100)") == CanonicalType.STRING
    assert canonicalize("TIMESTAMP_NTZ") == CanonicalType.TIMESTAMP
    assert canonicalize("boolean") == CanonicalType.BOOLEAN


def test_types_match_oracle_vs_snowflake():
    assert types_match("NUMBER(38,0)", "NUMBER(38,0)")
    assert types_match("VARCHAR2(50)", "VARCHAR(50)")
    assert not types_match("VARCHAR(10)", "NUMBER(10)")


def test_values_equal_normalization():
    # trailing spaces + case
    assert values_equal("abc ", "abc", CanonicalType.STRING, ignore_spaces=True)
    assert values_equal("ABC", "abc", CanonicalType.STRING, case_insensitive=True)
    # null / empty equivalence (Oracle ''=NULL) — but the literal text "null" is a REAL value,
    # not NULL (treating it as NULL caused silent false matches).
    assert values_equal(None, "", CanonicalType.STRING, null_equals_empty=True)
    assert not values_equal("null", None, CanonicalType.STRING, null_equals_empty=True)
    # numeric tolerance
    assert values_equal("100.00", 100.0, CanonicalType.NUMERIC)
    assert values_equal(100.0, 100.4, CanonicalType.NUMERIC, tolerance=0.5)
    assert not values_equal(100.0, 100.4, CanonicalType.NUMERIC, tolerance=0.1)


# ---- Cross-source value-comparison regressions (Snowflake↔Oracle correctness bugs) ----
def test_large_numeric_keys_do_not_collide():
    """Exact Decimal compare — 18+-digit ids/amounts differing in low digits must NOT be equal
    (a float round-trip collapsed them to the same value)."""
    assert not values_equal("123456789012345678", "123456789012345679", CanonicalType.NUMERIC)
    assert not values_equal("9999999999999999999999", "9999999999999999999998", CanonicalType.NUMERIC)
    # representational equivalences still hold
    assert values_equal("1.50", "1.5", CanonicalType.NUMERIC)
    assert values_equal("1e3", "1000", CanonicalType.NUMERIC)
    assert values_equal("00042", 42, CanonicalType.NUMERIC)


def test_boolean_vs_number_one():
    """Snowflake BOOLEAN vs Oracle NUMBER(1): True↔1 / False↔0 must compare equal."""
    assert canonicalize("NUMBER(1)") == CanonicalType.NUMERIC
    t = resolve_compare_type(CanonicalType.BOOLEAN, CanonicalType.NUMERIC)
    assert values_equal(True, 1, t)
    assert values_equal(False, 0, t)
    assert not values_equal(True, 0, t)
    # Y/N char flags vs boolean still work via the BOOLEAN branch
    assert values_equal("Y", True, resolve_compare_type(CanonicalType.STRING, CanonicalType.BOOLEAN))


def test_date_vs_timestamp_keeps_time():
    """A time-bearing value must NOT be silently collapsed to its date on a TIMESTAMP column."""
    d, noon = date(2026, 1, 1), datetime(2026, 1, 1, 12, 0)
    midnight = datetime(2026, 1, 1, 0, 0)
    assert not values_equal(d, noon, CanonicalType.TIMESTAMP)      # date != noon
    assert values_equal(d, midnight, CanonicalType.TIMESTAMP)      # date == midnight ts
    assert values_equal(d, noon, CanonicalType.DATE)               # DATE column → time ignored
    assert values_equal(d, noon, CanonicalType.TIMESTAMP, compare_time=False)  # opt-out
    # tz-aware same instant in different zones still equal
    from datetime import timedelta, timezone
    a = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    b = datetime(2026, 1, 1, 7, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert values_equal(a, b, CanonicalType.TIMESTAMP)


def test_literal_null_string_is_a_real_value():
    """The text "null" and whitespace-only strings are real values, not SQL NULL."""
    assert not values_equal("null", None, CanonicalType.STRING)
    assert not values_equal("NULL", None, CanonicalType.STRING)
    assert not values_equal("   ", None, CanonicalType.STRING)
    # Oracle ''=NULL still honored
    assert values_equal("", None, CanonicalType.STRING, null_equals_empty=True)
    assert not values_equal("", None, CanonicalType.STRING, null_equals_empty=False)
