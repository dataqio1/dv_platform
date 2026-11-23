"""Shared SQL canonicalization — the SINGLE source of truth for how each dialect renders a value
to the canonical TEXT that the pushdown compare engines hash (recursive_hash, snowflake_hash,
fingerprint_compare).

Keeping these in one place is a correctness requirement: if the engines render the "same" value
differently, equal rows hash differently (over-drilling, or — on paths without a leaf re-judge —
false differences), and if they collapse distinct values to the same text, different rows hash
equal and get pruned as a FALSE MATCH that is never re-judged. The rules here must stay aligned
with the authoritative Python comparator `typemap.values_equal`.
"""

from __future__ import annotations

# Oracle number format: fixed 10 decimals, no leading sign-space (FM), wide integer part. Paired
# with the trailing-zero strip below it yields the same text Snowflake's TO_VARCHAR(NUMBER(38,10))
# produces for an equal value.
_ORA_NUMFMT = "FM99999999999999999990.0000000000"


def numeric_text(dialect: str, expr: str) -> str:
    """Canonical exact TEXT for a numeric value.

    - **Full precision, NO float/DOUBLE round-trip** — so two distinct large ids/amounts (18–38
      digit NUMBER) never collapse to the same value and get falsely pruned as equal. (The old
      `CAST(... AS DOUBLE)` lost ~16 significant digits.)
    - **Trailing zeros after a decimal point are stripped** so `1.50` == `1.5` (matches
      `values_equal`'s Decimal equality); integers are left intact (`100` stays `100`).
    - **Byte-identical across Oracle↔Snowflake** for equal logical values, so the cross-engine
      fingerprint prunes equal rows instead of over-drilling.
    """
    d = (dialect or "").lower()
    if d == "oracle":
        return f"RTRIM(RTRIM(TO_CHAR(CAST({expr} AS NUMBER(38,10)),'{_ORA_NUMFMT}'),'0'),'.')"
    if d == "snowflake":
        return f"RTRIM(RTRIM(TO_VARCHAR(CAST({expr} AS NUMBER(38,10))),'0'),'.')"
    # sqlite / default: keep FULL integer precision (CAST AS TEXT, never REAL/float) and strip
    # trailing zeros only when there's a decimal point (so 100 stays 100, 1.50 → 1.5).
    t = f"CAST({expr} AS TEXT)"
    return f"CASE WHEN {t} LIKE '%.%' THEN RTRIM(RTRIM({t}, '0'), '.') ELSE {t} END"


def _to_text(dialect: str, expr: str) -> str:
    d = (dialect or "").lower()
    if d == "oracle":
        return f"TO_CHAR({expr})"
    if d == "snowflake":
        return f"TO_VARCHAR({expr})"
    return f"CAST({expr} AS TEXT)"


def string_text(
    dialect: str, expr: str, *, ignore_spaces: bool = True, case_insensitive: bool = False,
    null_equals_empty: bool = True,
) -> str:
    """Canonical TEXT for a string value under the same rules as `typemap.values_equal`: optional
    TRIM (ignore trailing/leading spaces), optional LOWER (case-insensitive), and NULLIF('')
    (Oracle's ''=NULL)."""
    e = _to_text(dialect, expr)
    if ignore_spaces:
        e = f"TRIM({e})"
    if case_insensitive:
        e = f"LOWER({e})"
    if null_equals_empty:
        e = f"NULLIF({e}, '')"
    return e


def temporal_text(dialect: str, expr: str, *, is_date: bool = False) -> str:
    """Canonical TEXT for a date/timestamp. A DATE renders as the calendar date; a TIMESTAMP renders
    to MICROSECOND (FF6) — matching Python datetime's precision and closing the sub-second gap where
    Snowflake TIMESTAMP_NTZ(9) vs Oracle TIMESTAMP(6) differences were truncated to whole seconds and
    falsely pruned as equal. The connectors pin the session time zone to UTC, so rendered strings are
    UTC-consistent across engines (TIMESTAMP_LTZ / Oracle WITH TIME ZONE no longer depend on ambient
    session config)."""
    d = (dialect or "").lower()
    fmt = "YYYY-MM-DD" if is_date else "YYYY-MM-DD HH24:MI:SS.FF6"
    if d == "oracle":
        return f"TO_CHAR({expr}, '{fmt}')"
    if d == "snowflake":
        return f"TO_VARCHAR({expr}, '{fmt}')"
    return f"CAST({expr} AS TEXT)"  # sqlite stores temporals as ISO text already


def boolean_text(dialect: str, expr: str) -> str:
    """Canonical 'true'/'false'/NULL for a boolean-ish value — so Snowflake BOOLEAN, Oracle
    NUMBER(1), and CHAR(1) Y/N flags all agree (mirrors `typemap._as_bool`).

    The operand is rendered to UPPER text FIRST, then matched against string tokens. Comparing a
    NUMBER column straight against a mixed IN-list (`0 IN (1,'Y',...)`) makes Oracle coerce the whole
    list to NUMBER → `ORA-01722: invalid number` on 'Y'/'T'. Text-first avoids that and is
    case-insensitive in one shot."""
    d = (dialect or "").lower()
    if d == "oracle":
        return (f"CASE WHEN {expr} IS NULL THEN NULL "
                f"WHEN UPPER(TO_CHAR({expr})) IN ('1','Y','T','TRUE') THEN 'true' ELSE 'false' END")
    if d == "snowflake":
        return (f"CASE WHEN {expr} IS NULL THEN NULL "
                f"WHEN UPPER(TO_VARCHAR({expr})) IN ('1','Y','T','TRUE') THEN 'true' ELSE 'false' END")
    return f"CAST({expr} AS TEXT)"  # sqlite: same-engine both sides → consistent
