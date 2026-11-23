"""Centralization guard: every pushdown compare engine MUST render a value through `sql_canon`.

These lock the dispatch so the three engines can't silently drift back to private per-engine
canonicalizers (the exact bug the audit found). We don't re-assert what the text *is* here —
test_sql_canon.py owns that — only that each engine delegates to the shared function for each
canonical type and each dialect.
"""

from __future__ import annotations

import pytest

from dvp.testing_engine import fingerprint_compare, recursive_hash, snowflake_hash, sql_canon
from dvp.testing_engine.rowcompare import CompareOptions
from dvp.testing_engine.typemap import CanonicalType

COL = 'Weird "Col"'  # embedded quote exercises identifier escaping too
Q = '"' + COL.replace('"', '""') + '"'
OPTS = CompareOptions(ignore_spaces=True, case_insensitive=True, null_equals_empty=True)


def _expected(dialect: str, canon: CanonicalType) -> str:
    if canon == CanonicalType.NUMERIC:
        return sql_canon.numeric_text(dialect, Q)
    if canon in (CanonicalType.DATE, CanonicalType.TIMESTAMP):
        return sql_canon.temporal_text(dialect, Q, is_date=(canon == CanonicalType.DATE))
    if canon == CanonicalType.BOOLEAN:
        return sql_canon.boolean_text(dialect, Q)
    return sql_canon.string_text(
        dialect, Q, ignore_spaces=OPTS.ignore_spaces,
        case_insensitive=OPTS.case_insensitive, null_equals_empty=OPTS.null_equals_empty,
    )


@pytest.mark.parametrize("dialect", ["oracle", "snowflake", "sqlite"])
@pytest.mark.parametrize(
    "canon",
    [CanonicalType.NUMERIC, CanonicalType.DATE, CanonicalType.TIMESTAMP,
     CanonicalType.BOOLEAN, CanonicalType.STRING],
)
def test_fingerprint_xnorm_delegates_to_sql_canon(dialect, canon):
    # fingerprint is the cross-engine path — it specializes all four canonical types.
    assert fingerprint_compare._xnorm(dialect, COL, canon, OPTS) == _expected(dialect, canon)


@pytest.mark.parametrize("dialect", ["oracle", "snowflake", "sqlite"])
@pytest.mark.parametrize("canon", [CanonicalType.NUMERIC, CanonicalType.STRING])
def test_recursive_hash_norm_delegates_to_sql_canon(dialect, canon):
    # recursive_hash (same-DB) handles numeric exactly; everything else as string text.
    expected = (
        sql_canon.numeric_text(dialect, Q) if canon == CanonicalType.NUMERIC
        else sql_canon.string_text(
            dialect, Q, ignore_spaces=OPTS.ignore_spaces,
            case_insensitive=OPTS.case_insensitive, null_equals_empty=OPTS.null_equals_empty)
    )
    assert recursive_hash._norm(dialect, COL, canon, OPTS) == expected


@pytest.mark.parametrize("canon", [CanonicalType.NUMERIC, CanonicalType.STRING])
def test_snowflake_hash_norm_delegates_to_sql_canon(canon):
    expected = (
        sql_canon.numeric_text("snowflake", Q) if canon == CanonicalType.NUMERIC
        else sql_canon.string_text(
            "snowflake", Q, ignore_spaces=OPTS.ignore_spaces,
            case_insensitive=OPTS.case_insensitive, null_equals_empty=OPTS.null_equals_empty)
    )
    assert snowflake_hash._norm_expr(COL, canon, OPTS) == expected


def test_date_and_timestamp_render_differently():
    # Guards the sub-second fix: a TIMESTAMP must carry FF6; a DATE must not. If someone
    # collapses them back to a shared seconds-only format, this fails.
    for dialect in ("oracle", "snowflake"):
        ts = fingerprint_compare._xnorm(dialect, COL, CanonicalType.TIMESTAMP, OPTS)
        dt = fingerprint_compare._xnorm(dialect, COL, CanonicalType.DATE, OPTS)
        assert "FF6" in ts and "FF6" not in dt
