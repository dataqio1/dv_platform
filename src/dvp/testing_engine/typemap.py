"""Canonical type system + value normalization for cross-DB compares.

Maps dialect-specific types (Oracle NUMBER, Snowflake NUMBER/VARCHAR2, etc.) to a
small canonical set and normalizes values before comparison, so representational
differences don't produce false mismatches (docs/design/05 §6).
"""

from __future__ import annotations

from datetime import UTC
from decimal import Decimal, InvalidOperation
from enum import StrEnum


class CanonicalType(StrEnum):
    NUMERIC = "NUMERIC"
    STRING = "STRING"
    DATE = "DATE"
    TIMESTAMP = "TIMESTAMP"
    BOOLEAN = "BOOLEAN"
    UNKNOWN = "UNKNOWN"


_NUMERIC = {
    "number",
    "numeric",
    "decimal",
    "int",
    "integer",
    "bigint",
    "smallint",
    "float",
    "double",
    "real",
    "binary_float",
    "binary_double",
}
_STRING = {"varchar", "varchar2", "char", "nchar", "nvarchar", "text", "string", "clob"}
_DATE = {"date"}
_TIMESTAMP = {"timestamp", "timestamp_tz", "timestamp_ltz", "timestamp_ntz", "datetime"}
_BOOLEAN = {"boolean", "bool", "bit"}


def canonicalize(raw_type: str) -> CanonicalType:
    base = raw_type.strip().lower().split("(")[0].strip()
    if base in _NUMERIC:
        return CanonicalType.NUMERIC
    if base in _STRING:
        return CanonicalType.STRING
    if base in _TIMESTAMP:
        return CanonicalType.TIMESTAMP
    if base in _DATE:
        return CanonicalType.DATE
    if base in _BOOLEAN:
        return CanonicalType.BOOLEAN
    return CanonicalType.UNKNOWN


def types_match(src_type: str, dst_type: str) -> bool:
    """Canonical equality — NUMBER(38,0) vs NUMBER(38,0), VARCHAR2 vs VARCHAR, etc."""
    return canonicalize(src_type) == canonicalize(dst_type)


# How "structured" each canonical type is — used to pick the comparison semantics when the two
# sides of a column carry different types (a NUMBER column vs a VARCHAR holding "123.00", a DATE
# vs a TIMESTAMP, …). The more-structured type wins so representational differences don't read as
# real differences, while genuinely incompatible pairs fall back to a safe string compare.
_RANK = {
    CanonicalType.NUMERIC: 5,
    CanonicalType.TIMESTAMP: 4,
    CanonicalType.DATE: 3,
    CanonicalType.BOOLEAN: 2,
    CanonicalType.STRING: 1,
    CanonicalType.UNKNOWN: 0,
}
# Pairs that are NOT meaningfully coercible — compare them as strings (they'll differ honestly,
# never silently "match" by forcing one into the other's type).
_INCOMPATIBLE = {
    frozenset({CanonicalType.NUMERIC, CanonicalType.TIMESTAMP}),
    frozenset({CanonicalType.NUMERIC, CanonicalType.DATE}),
    frozenset({CanonicalType.BOOLEAN, CanonicalType.TIMESTAMP}),
    frozenset({CanonicalType.BOOLEAN, CanonicalType.DATE}),
}


def resolve_compare_type(a: CanonicalType, b: CanonicalType) -> CanonicalType:
    """Pick the canonical type to compare a column by, given each side's type.

    Same type → that type. DATE vs TIMESTAMP → TIMESTAMP (date-aware). A structured type (numeric/
    temporal/boolean) vs STRING/UNKNOWN → the structured one (so "123.00" matches 123, and a
    timestamp string matches a TIMESTAMP). Genuinely incompatible pairs → STRING.
    """
    if a == b:
        return a
    if frozenset({a, b}) in _INCOMPATIBLE:
        return CanonicalType.STRING
    # otherwise the more-structured type drives the comparison
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


def values_equal(
    src,  # noqa: ANN001
    dst,  # noqa: ANN001
    canonical: CanonicalType,
    *,
    ignore_spaces: bool = True,
    null_equals_empty: bool = True,
    case_insensitive: bool = False,
    tolerance: float = 0.0,
    compare_time: bool = True,
) -> bool:
    """Compare two raw values under the configured normalization rules. `canonical` should be the
    resolved comparison type for the column pair (see resolve_compare_type)."""
    if _is_nullish(src, null_equals_empty) and _is_nullish(dst, null_equals_empty):
        return True
    if _is_nullish(src, null_equals_empty) != _is_nullish(dst, null_equals_empty):
        return False

    if canonical == CanonicalType.NUMERIC:
        a, b = _as_number(src), _as_number(dst)
        if a is None or b is None:
            return str(src).strip() == str(dst).strip()
        # Exact Decimal comparison when tolerance is 0 — a float round-trip would collapse two
        # different 18+-digit NUMBER(38,0) ids/amounts to the same value. 1.50 == 1.5 still holds.
        if tolerance:
            try:
                return abs(a - b) <= Decimal(str(tolerance))
            except InvalidOperation:
                return abs(float(a) - float(b)) <= tolerance
        return a == b

    if canonical in (CanonicalType.DATE, CanonicalType.TIMESTAMP):
        return _temporal_equal(src, dst, canonical, tolerance, compare_time)

    if canonical == CanonicalType.BOOLEAN:
        sb, db = _as_bool(src), _as_bool(dst)
        if sb is None or db is None:  # not boolean-ish on one side → honest string compare
            return str(src).strip() == str(dst).strip()
        return sb == db

    # STRING / UNKNOWN
    a, b = str(src), str(dst)
    if ignore_spaces:
        a, b = a.strip(), b.strip()
    if case_insensitive:
        a, b = a.lower(), b.lower()
    return a == b


def _is_nullish(v, null_equals_empty: bool) -> bool:  # noqa: ANN001
    # Only real NULL, or (under Oracle's ''=NULL rule) a genuinely empty string. We deliberately do
    # NOT treat the literal text "null" or whitespace-only strings as NULL — those are real values a
    # column may legitimately hold, and conflating them causes silent false matches.
    if v is None:
        return True
    return bool(null_equals_empty and isinstance(v, str) and v == "")


def _as_number(v) -> Decimal | None:  # noqa: ANN001
    # Parse to Decimal (exact) rather than float, so large ids/amounts don't lose low digits.
    # Booleans coerce to 1/0 so a Snowflake BOOLEAN compares equal to an Oracle NUMBER(1).
    if isinstance(v, bool):
        return Decimal(1) if v else Decimal(0)
    try:
        return Decimal(str(v).strip())
    except (TypeError, ValueError, InvalidOperation):
        return None


_TRUE = {"1", "1.0", "true", "t", "yes", "y"}
_FALSE = {"0", "0.0", "false", "f", "no", "n"}


def _as_bool(v) -> bool | None:  # noqa: ANN001
    """Parse boolean-ish values across engines: true/1/t/yes/y and false/0/f/no/n. Anything
    else → None (not boolean), so the caller can fall back rather than mis-coerce."""
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return None


def _parse_temporal(v):  # noqa: ANN001
    """Best-effort parse of a date/datetime from a datetime, date, or common string form.
    Returns a datetime|date or None. Naive datetimes are treated as UTC by the caller."""
    from datetime import date, datetime

    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    iso = s.replace("Z", "+00:00")
    if "T" not in iso and " " in iso:
        iso = iso.replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _temporal_equal(  # noqa: ANN001
    src, dst, canonical: CanonicalType, tolerance: float, compare_time: bool = True,
) -> bool:
    from datetime import date, datetime

    a, b = _parse_temporal(src), _parse_temporal(dst)
    if a is None or b is None:  # unparseable → honest normalized-string compare
        return str(src).strip() == str(dst).strip()

    def as_date(x):  # noqa: ANN001, ANN202
        return x if isinstance(x, date) and not isinstance(x, datetime) else x.date()

    def as_dt(x):  # noqa: ANN001, ANN202 — promote a bare date to midnight so a date matches a
        return x if isinstance(x, datetime) else datetime(x.year, x.month, x.day)  # midnight ts

    # Compare the calendar date only when the column is a DATE type (that's the declared intent, so
    # an Oracle DATE's incidental time-of-day is ignored) or the user opted out of time. Otherwise
    # compare the full instant — so a bare date matches a MIDNIGHT timestamp but NOT a time-bearing
    # one (previously a time-bearing value was silently collapsed to its date and reported equal).
    if canonical == CanonicalType.DATE or not compare_time:
        return as_date(a) == as_date(b)
    a, b = as_dt(a), as_dt(b)
    a = a.replace(tzinfo=UTC) if a.tzinfo is None else a.astimezone(UTC)
    b = b.replace(tzinfo=UTC) if b.tzinfo is None else b.astimezone(UTC)
    return abs((a - b).total_seconds()) <= tolerance
