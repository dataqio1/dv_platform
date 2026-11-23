"""DQ rule catalog.

Each rule renders an aggregate SQL fragment (`to_sql`) and evaluates the returned
aggregates in Python (`evaluate`). Because every rule contributes a fragment, a
monitor batches all of a table's rules into ONE pushdown query regardless of rule
count (docs/design/04 §3.2, scaling D11). This module focuses on the fragment +
evaluation contract; the profiler assembles and runs the combined query.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class RuleType(StrEnum):
    NOT_NULL = "not_null"
    NULL_RATE = "null_rate"
    ROW_COUNT = "row_count"
    UNIQUE = "unique"
    RANGE = "range"
    IN_SET = "in_set"
    REGEX = "regex"
    FRESHNESS = "freshness"
    CUSTOM_SQL = "custom_sql"
    # SCD2 load-aware checks (docs/design/11). Active rows are selected by config["active_sql"].
    SCD2_ACTIVE_UNIQUE = "scd2_active_unique"  # ≤1 active version per business key
    SCD2_NO_OVERLAP = "scd2_no_overlap"        # version [from,to) intervals don't overlap per key


@dataclass
class RuleResultValue:
    passed: bool
    metric_value: float | None
    threshold: float | None
    observed: dict


class Rule(Protocol):
    type: RuleType

    def to_sql(self, table: str, predicate: str | None) -> dict[str, str]:
        """Return {alias: sql_expression} fragments to add to the combined SELECT."""
        ...

    def evaluate(self, aggregates: dict) -> RuleResultValue: ...

    @property
    def requires_full_scan(self) -> bool: ...


def _where(predicate: str | None) -> str:
    return f" WHERE {predicate}" if predicate else ""


@dataclass
class NotNullRule:
    column: str
    type: RuleType = RuleType.NOT_NULL

    @property
    def requires_full_scan(self) -> bool:
        return False

    def to_sql(self, table: str, predicate: str | None) -> dict[str, str]:
        alias = f"nulls__{self.column}"
        return {alias: f"SUM(CASE WHEN {self.column} IS NULL THEN 1 ELSE 0 END)"}

    def evaluate(self, aggregates: dict) -> RuleResultValue:
        nulls = float(aggregates.get(f"nulls__{self.column}", 0) or 0)
        return RuleResultValue(
            passed=nulls == 0, metric_value=nulls, threshold=0, observed={"null_count": nulls}
        )


@dataclass
class NullRateRule:
    column: str
    max_rate: float  # e.g. 0.01 == 1%
    type: RuleType = RuleType.NULL_RATE

    @property
    def requires_full_scan(self) -> bool:
        return False

    def to_sql(self, table: str, predicate: str | None) -> dict[str, str]:
        return {
            f"nulls__{self.column}": f"SUM(CASE WHEN {self.column} IS NULL THEN 1 ELSE 0 END)",
            "row_count": "COUNT(*)",
        }

    def evaluate(self, aggregates: dict) -> RuleResultValue:
        rows = float(aggregates.get("row_count", 0) or 0)
        nulls = float(aggregates.get(f"nulls__{self.column}", 0) or 0)
        rate = (nulls / rows) if rows else 0.0
        return RuleResultValue(
            passed=rate <= self.max_rate,
            metric_value=rate,
            threshold=self.max_rate,
            observed={"null_rate": rate, "rows": rows},
        )


@dataclass
class RowCountRule:
    min_rows: int = 1
    type: RuleType = RuleType.ROW_COUNT

    @property
    def requires_full_scan(self) -> bool:
        return False

    def to_sql(self, table: str, predicate: str | None) -> dict[str, str]:
        return {"row_count": "COUNT(*)"}

    def evaluate(self, aggregates: dict) -> RuleResultValue:
        rows = float(aggregates.get("row_count", 0) or 0)
        return RuleResultValue(
            passed=rows >= self.min_rows,
            metric_value=rows,
            threshold=float(self.min_rows),
            observed={"rows": rows},
        )


@dataclass
class RangeRule:
    column: str
    min_value: float | None = None
    max_value: float | None = None
    type: RuleType = RuleType.RANGE

    @property
    def requires_full_scan(self) -> bool:
        return False

    def to_sql(self, table: str, predicate: str | None) -> dict[str, str]:
        parts = []
        if self.min_value is not None:
            parts.append(f"{self.column} < {self.min_value}")
        if self.max_value is not None:
            parts.append(f"{self.column} > {self.max_value}")
        cond = " OR ".join(parts) if parts else "1=0"
        return {f"outofrange__{self.column}": f"SUM(CASE WHEN {cond} THEN 1 ELSE 0 END)"}

    def evaluate(self, aggregates: dict) -> RuleResultValue:
        bad = float(aggregates.get(f"outofrange__{self.column}", 0) or 0)
        return RuleResultValue(
            passed=bad == 0, metric_value=bad, threshold=0, observed={"out_of_range": bad}
        )


@dataclass
class UniqueRule:
    columns: list[str]
    type: RuleType = RuleType.UNIQUE

    @property
    def requires_full_scan(self) -> bool:
        # Uniqueness is the documented exception to delta-only analysis
        # (docs/design/04 §4.4): a new row may duplicate an OLD row.
        return True

    def to_sql(self, table: str, predicate: str | None) -> dict[str, str]:
        cols = ", ".join(self.columns)
        # Pushdown: COUNT(*) - COUNT(DISTINCT key); no rows pulled. Ignores predicate
        # on purpose — uniqueness must scan beyond the delta.
        return {
            "total_rows_full": f"(SELECT COUNT(*) FROM {table})",
            "distinct_key_full": f"(SELECT COUNT(*) FROM (SELECT DISTINCT {cols} FROM {table}))",
        }

    def evaluate(self, aggregates: dict) -> RuleResultValue:
        total = float(aggregates.get("total_rows_full", 0) or 0)
        distinct = float(aggregates.get("distinct_key_full", 0) or 0)
        dupes = total - distinct
        return RuleResultValue(
            passed=dupes == 0,
            metric_value=dupes,
            threshold=0,
            observed={"duplicate_rows": dupes, "key": self.columns},
        )
