"""Three-level DQ rule auto-generation (docs/design/04 §3.4).

Given a column profile, propose rules calibrated to the observed data. Levels are
cumulative: 1=Essential, 2=Standard, 3=Comprehensive. Generated rules are returned
as `suggested` specs with a confidence; a human accepts them before they raise
incidents. This module is pure (profile in → rule specs out) so it is trivially
unit-testable without a warehouse.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dvp.observability.rules import RuleType


@dataclass
class ColumnProfile:
    name: str
    data_type: str  # canonical: NUMERIC | STRING | DATE | TIMESTAMP | BOOLEAN
    null_rate: float = 0.0
    distinct_ratio: float = 0.0  # distinct / rows
    min_value: float | None = None
    max_value: float | None = None
    is_key_candidate: bool = False
    looks_like_email: bool = False


@dataclass
class TableProfile:
    table: str
    row_count: int
    columns: list[ColumnProfile] = field(default_factory=list)
    load_ts_column: str | None = None
    detected_key: list[str] = field(default_factory=list)


def generate_rules(profile: TableProfile, level: int, *, null_margin: float = 0.02) -> list[dict]:
    if level not in (1, 2, 3):
        raise ValueError("level must be 1, 2, or 3")

    specs: list[dict] = []

    # ---- Level 1: Essential ----
    specs.append(
        _spec(
            RuleType.ROW_COUNT, name="row_count_positive", config={"min_rows": 1}, confidence=0.99
        )
    )
    for col in profile.columns:
        if col.null_rate == 0.0:
            specs.append(
                _spec(
                    RuleType.NOT_NULL,
                    name=f"not_null__{col.name}",
                    columns=[col.name],
                    confidence=0.95,
                )
            )
    if profile.load_ts_column:
        specs.append(
            _spec(
                RuleType.FRESHNESS,
                name=f"freshness__{profile.load_ts_column}",
                columns=[profile.load_ts_column],
                config={"max_age_hours": 24},
                confidence=0.8,
            )
        )
    if level == 1:
        return specs

    # ---- Level 2: Standard ----
    for col in profile.columns:
        if 0.0 < col.null_rate < 1.0:
            threshold = round(min(1.0, col.null_rate + null_margin), 4)
            specs.append(
                _spec(
                    RuleType.NULL_RATE,
                    name=f"null_rate__{col.name}",
                    columns=[col.name],
                    config={"max_rate": threshold},
                    confidence=0.7,
                )
            )
        if col.data_type == "NUMERIC" and col.min_value is not None and col.max_value is not None:
            specs.append(
                _spec(
                    RuleType.RANGE,
                    name=f"range__{col.name}",
                    columns=[col.name],
                    config={"min_value": col.min_value, "max_value": col.max_value},
                    confidence=0.6,
                )
            )
    if profile.detected_key:
        specs.append(
            _spec(
                RuleType.UNIQUE,
                name="unique__key",
                columns=profile.detected_key,
                config={},
                scope="full",
                confidence=0.75,
            )
        )
    if level == 2:
        return specs

    # ---- Level 3: Comprehensive ----
    for col in profile.columns:
        if col.looks_like_email:
            specs.append(
                _spec(
                    RuleType.REGEX,
                    name=f"regex_email__{col.name}",
                    columns=[col.name],
                    config={"pattern": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"},
                    confidence=0.6,
                )
            )
    return specs


def _spec(
    rule_type: RuleType,
    *,
    name: str,
    columns: list[str] | None = None,
    config: dict | None = None,
    scope: str = "incremental",
    confidence: float = 0.5,
) -> dict:
    return {
        "name": name,
        "type": rule_type.value,
        "columns": columns or [],
        "config": config or {},
        "severity": "warning",
        "scope": scope,
        "state": "suggested",
        "confidence": confidence,
    }
