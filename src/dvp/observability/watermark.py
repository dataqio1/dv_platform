"""Watermark-column detection & validation (pure logic).

A *watermark column* is a monotonically increasing column (typically a load/update
timestamp) that lets a monitor process only new rows past each table's high-water-mark
instead of full-scanning. It is set once at the monitor level and is expected to exist
in every selected table (see the "Edit Monitor → Watermark" UI).

This module is pure — schema in (column metadata per table), suggestions/verdict out —
so it is trivially unit-testable without a warehouse. The service layer
(`dvp.services.watermark`) fetches the real schemas via a connector and calls these.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Canonical temporal buckets we accept as watermarks.
_TIMESTAMP_HINTS = ("TIMESTAMP", "DATETIME")
_DATE_HINTS = ("DATE",)
_TIME_HINTS = ("TIME",)
_NUMERIC_HINTS = ("INT", "NUMBER", "NUMERIC", "DECIMAL", "BIGINT", "SERIAL")

# Name heuristics, highest-signal first. Each maps a set of lowercased name patterns
# to a base score. A column whose name matches a higher tier is a stronger candidate.
_NAME_TIERS: list[tuple[int, tuple[str, ...]]] = [
    (
        100,
        (
            "ingested_at",
            "ingestion_time",
            "loaded_at",
            "load_ts",
            "load_timestamp",
            "load_date",
            "etl_loaded_at",
            "dw_loaded_at",
            "_loaded_at",
            "dbt_loaded_at",
        ),
    ),
    (
        92,
        (
            "updated_at",
            "modified_at",
            "last_modified",
            "last_updated",
            "update_ts",
            "updated_ts",
            "modified_ts",
            "last_update_date",
        ),
    ),
    (
        84,
        (
            "created_at",
            "created_ts",
            "create_date",
            "inserted_at",
            "insert_ts",
            "record_created_at",
        ),
    ),
    (74, ("event_time", "event_ts", "event_timestamp", "occurred_at", "processed_at")),
]


def canonical_temporal_type(raw_type: str) -> str | None:
    """Map a raw source type to timestamp|date|time|numeric, or None if not usable
    as a watermark."""
    t = (raw_type or "").upper()
    if any(h in t for h in _TIMESTAMP_HINTS):
        return "timestamp"
    if any(h in t for h in _DATE_HINTS):
        return "date"
    if any(h in t for h in _TIME_HINTS):
        return "time"
    if any(h in t for h in _NUMERIC_HINTS):
        return "numeric"
    return None


def _name_score(name: str) -> int:
    low = name.lower()
    for score, patterns in _NAME_TIERS:
        if any(p == low or low.endswith(p) or p in low for p in patterns):
            return score
    # Generic temporal-looking names still get a modest score.
    if low.endswith(("_at", "_ts", "_date", "_time", "_timestamp")):
        return 55
    if low.startswith(("date", "time", "ts_")):
        return 45
    return 25


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool = True


@dataclass
class WatermarkSuggestion:
    column: str
    type: str  # timestamp|date|numeric (preferred canonical type across tables)
    confidence: float  # 0..1
    present_in: int
    total_tables: int
    nullable_in_any: bool
    reason: str

    def as_dict(self) -> dict:
        return {
            "column": self.column,
            "type": self.type,
            "confidence": round(self.confidence, 3),
            "present_in": self.present_in,
            "total_tables": self.total_tables,
            "covers_all_tables": self.present_in == self.total_tables,
            "nullable_in_any": self.nullable_in_any,
            "reason": self.reason,
        }


def detect_watermark(
    schemas: dict[str, list[ColumnInfo]],
) -> list[WatermarkSuggestion]:
    """Rank candidate watermark columns across the selected tables.

    `schemas` maps a table reference (any label) to its columns. We prefer columns
    that (a) are temporal, (b) are present in *every* table, (c) have a high-signal
    name, and (d) are non-nullable. Suggestions are returned best-first.
    """
    total = len(schemas)
    if total == 0:
        return []

    # Aggregate per column-name (case-insensitive) across tables.
    agg: dict[str, dict] = {}
    for _table, cols in schemas.items():
        seen_here: set[str] = set()
        for c in cols:
            canon = canonical_temporal_type(c.data_type)
            if canon is None:
                continue
            key = c.name.lower()
            if key in seen_here:
                continue
            seen_here.add(key)
            entry = agg.setdefault(
                key,
                {"display": c.name, "present": 0, "types": set(), "nullable_any": False},
            )
            entry["present"] += 1
            entry["types"].add(canon)
            entry["nullable_any"] = entry["nullable_any"] or c.nullable

    suggestions: list[WatermarkSuggestion] = []
    for entry in agg.values():
        display = entry["display"]
        present = entry["present"]
        coverage = present / total
        name_score = _name_score(display)
        # Coverage dominates: a column missing from tables is a poor monitor-level
        # watermark even if its name is perfect.
        nonnull_bonus = 0 if entry["nullable_any"] else 10
        raw = (name_score + nonnull_bonus) * coverage
        confidence = max(0.0, min(1.0, raw / 110.0))
        # Preferred type: timestamp > date > numeric > time.
        types = entry["types"]
        wtype = next(
            (t for t in ("timestamp", "date", "numeric", "time") if t in types), "timestamp"
        )
        reason = _reason(display, name_score, present, total, entry["nullable_any"])
        suggestions.append(
            WatermarkSuggestion(
                column=display,
                type=wtype,
                confidence=confidence,
                present_in=present,
                total_tables=total,
                nullable_in_any=entry["nullable_any"],
                reason=reason,
            )
        )

    # Best first: full coverage, then confidence, then name.
    suggestions.sort(
        key=lambda s: (s.present_in == s.total_tables, s.confidence, s.column.lower()), reverse=True
    )
    return suggestions


def _reason(name: str, name_score: int, present: int, total: int, nullable: bool) -> str:
    bits: list[str] = []
    if name_score >= 92:
        bits.append("name strongly indicates a load/update timestamp")
    elif name_score >= 74:
        bits.append("name indicates a creation/event timestamp")
    elif name_score >= 45:
        bits.append("temporal-looking name")
    else:
        bits.append("temporal column")
    if present == total:
        bits.append(f"present in all {total} table(s)")
    else:
        bits.append(f"present in {present}/{total} table(s)")
    if nullable:
        bits.append("nullable — rows with NULL could be skipped")
    return "; ".join(bits)


# ----------------------------- validation -----------------------------
@dataclass
class TableWatermarkCheck:
    table: str
    exists: bool
    actual_type: str | None = None
    canonical_type: str | None = None
    is_temporal: bool = False
    type_matches: bool = False
    nullable: bool = False
    # Hard blockers (column missing / wrong type) fail the table. Soft warnings (e.g. a
    # nullable watermark) are surfaced but do NOT invalidate — the watermark is still usable.
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "table": self.table,
            "exists": self.exists,
            "actual_type": self.actual_type,
            "canonical_type": self.canonical_type,
            "is_temporal": self.is_temporal,
            "type_matches": self.type_matches,
            "nullable": self.nullable,
            "ok": self.exists and self.type_matches and not self.issues,
            "issues": self.issues,
            "warnings": self.warnings,
        }


def validate_watermark(
    schemas: dict[str, list[ColumnInfo]],
    column: str,
    expected_type: str | None = None,
) -> dict:
    """Check that `column` exists in every table and is a usable watermark.

    Returns a per-table breakdown plus an overall verdict. `expected_type`, if given
    (timestamp|date|numeric), is enforced against each table's canonical type.
    """
    checks: list[TableWatermarkCheck] = []
    want = expected_type.lower() if expected_type else None
    for table, cols in schemas.items():
        match = next((c for c in cols if c.name.lower() == column.lower()), None)
        if match is None:
            checks.append(
                TableWatermarkCheck(table=table, exists=False, issues=["column not found"])
            )
            continue
        canon = canonical_temporal_type(match.data_type)
        is_temporal = canon in ("timestamp", "date", "time")
        issues: list[str] = []
        warnings: list[str] = []
        if want is not None:
            type_matches = canon == want
            if not type_matches:
                issues.append(f"expected {want} but column is {canon or match.data_type}")
        else:
            type_matches = canon is not None
            if canon is None:
                issues.append("not a timestamp/date/numeric column")
        # Nullable is a soft warning, not a blocker: the column is still a valid watermark,
        # we just note that rows with a NULL value could be missed on an incremental pass.
        if match.nullable:
            warnings.append("column is nullable — rows with NULL watermark may be skipped")
        checks.append(
            TableWatermarkCheck(
                table=table,
                exists=True,
                actual_type=match.data_type,
                canonical_type=canon,
                is_temporal=is_temporal,
                type_matches=type_matches,
                nullable=match.nullable,
                issues=issues,
                warnings=warnings,
            )
        )

    total = len(checks)
    with_col = sum(1 for c in checks if c.exists)
    ok_tables = sum(1 for c in checks if c.as_dict()["ok"])
    tables_with_warnings = sum(1 for c in checks if c.warnings)
    valid = total > 0 and with_col == total and ok_tables == total
    return {
        "column": column,
        "expected_type": expected_type,
        "valid": valid,
        "tables_total": total,
        "tables_with_column": with_col,
        "tables_ok": ok_tables,
        "tables_with_warnings": tables_with_warnings,
        "missing_in": [c.table for c in checks if not c.exists],
        "tables": [c.as_dict() for c in checks],
    }
