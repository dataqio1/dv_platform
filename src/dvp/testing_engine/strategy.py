"""Compare strategy selector (docs/design/05 §7). Picks the cheapest correct path.

Pure decision logic so it is unit-testable without any DB. The actual execution of
each strategy (EXCEPT pushdown, Spark hash/greedy) lives in compare.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CompareStrategy(StrEnum):
    SERVER_SIDE_EXCEPT = "server_side_except"
    PUSHDOWN_EXCEPT_JOIN = "pushdown_except_join"
    SPARK_ADAPTIVE_RANGE = "spark_adaptive_range"
    SPARK_HASH_GREEDY = "spark_hash_greedy"


@dataclass
class CompareInputs:
    same_snowflake_account: bool
    same_dialect: bool
    dialect_supports_pushdown: bool
    key_is_integer: bool
    has_transform_or_tolerance_or_filter: bool
    adaptive_enabled: bool = True


def choose_strategy(inp: CompareInputs) -> CompareStrategy:
    if inp.same_snowflake_account and not inp.has_transform_or_tolerance_or_filter:
        return CompareStrategy.SERVER_SIDE_EXCEPT
    if (
        inp.same_dialect
        and inp.dialect_supports_pushdown
        and not inp.has_transform_or_tolerance_or_filter
    ):
        return CompareStrategy.PUSHDOWN_EXCEPT_JOIN
    if inp.key_is_integer and inp.adaptive_enabled:
        return CompareStrategy.SPARK_ADAPTIVE_RANGE
    return CompareStrategy.SPARK_HASH_GREEDY
