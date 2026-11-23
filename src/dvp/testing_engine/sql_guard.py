"""Validate the user-supplied SQL fragments carried by a compare side.

A compare side may bring a ``filter`` (a WHERE predicate) and/or a raw ``sql`` (a SELECT used as
the dataset). Both are interpolated into generated SQL, so we vet them before they reach a
connector:

* a **filter** must be a single boolean predicate — no statement terminator (``;``), no SQL
  comment (``--``, ``/* */``, ``#``), and no set-operator piggybacking (``UNION``/``INTERSECT``/
  ``EXCEPT``) or DDL/DML keyword that would turn ``WHERE <pred>`` into a second statement;
* a raw **sql** side must be a single read-only ``SELECT``/``WITH`` — no stacked statements, no
  comments.

Connections are already tenant-scoped (a user can only target their own warehouse), so this is
least-privilege / defense-in-depth, not the only line of defense. String literals are blanked
before scanning so a legitimate value like ``name LIKE '%union%'`` or ``note = 'a;b'`` is allowed.
"""

from __future__ import annotations

import re

from dvp.core.errors import ValidationError

# Blank single-quoted string literals ('' escapes an embedded quote) so their contents never trip
# the keyword/terminator scans below.
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_COMMENT = re.compile(r"--|/\*|\*/|#")
_SET_OPERATOR = re.compile(r"(?i)\b(?:UNION|INTERSECT|EXCEPT)\b")
_STATEMENT_KEYWORD = re.compile(
    r"(?i)\b(?:DROP|DELETE|UPDATE|INSERT|ALTER|CREATE|GRANT|REVOKE|TRUNCATE|MERGE|CALL|"
    r"EXEC|EXECUTE|ATTACH|DETACH|PRAGMA|VACUUM|REPLACE|COPY)\b"
)
_LEADS_WITH_SELECT = re.compile(r"(?i)^\(*\s*(?:SELECT|WITH)\b")


def _scrub(fragment: str) -> str:
    return _STRING_LITERAL.sub("''", fragment)


def validate_filter(predicate: str | None, *, side: str = "source") -> str | None:
    """Return the predicate unchanged if it's a safe single boolean expression, else raise
    ValidationError. ``None``/empty is fine (no filter)."""
    if predicate is None:
        return None
    pred = predicate.strip()
    if not pred:
        return predicate
    scrubbed = _scrub(pred)
    detail = {"side": side, "filter": predicate}
    if ";" in scrubbed:
        raise ValidationError("filter may not contain ';' (one predicate only)", details=detail)
    if _COMMENT.search(scrubbed):
        raise ValidationError("filter may not contain SQL comments", details=detail)
    if _SET_OPERATOR.search(scrubbed):
        raise ValidationError("filter may not contain UNION/INTERSECT/EXCEPT", details=detail)
    if _STATEMENT_KEYWORD.search(scrubbed):
        raise ValidationError("filter may not contain SQL statement keywords", details=detail)
    return predicate


def validate_subquery_sql(sql: str | None, *, side: str = "source") -> str | None:
    """Return the raw-SQL side unchanged if it's a single read-only SELECT/WITH, else raise.
    ``None``/empty is fine (the side is a plain table instead)."""
    if sql is None:
        return None
    raw = sql.strip()
    if not raw:
        return sql
    body = raw.rstrip(";").strip()  # a single trailing ';' is allowed and stripped elsewhere
    scrubbed = _scrub(body)
    detail = {"side": side, "sql": sql}
    if ";" in scrubbed:
        raise ValidationError("sql must be a single statement (no ';')", details=detail)
    if _COMMENT.search(scrubbed):
        raise ValidationError("sql may not contain SQL comments", details=detail)
    if not _LEADS_WITH_SELECT.match(scrubbed):
        raise ValidationError("sql must be a read-only SELECT/WITH query", details=detail)
    return sql


def validate_compare_side(spec: dict | None, *, side: str = "source") -> None:
    """Vet the ``filter`` and ``sql`` of one compare side (a no-op for table-only sides)."""
    if not spec:
        return
    validate_filter(spec.get("filter"), side=side)
    validate_subquery_sql(spec.get("sql"), side=side)
