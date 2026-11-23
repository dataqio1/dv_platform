"""Compare-key detection (docs/design/05 §4).

Two-tier approach ported from the datadiff engine: (1) filter keyable columns and
score by name priority, (2) greedily build a composite from a sample, then confirm
on the full table via pushdown COUNT(DISTINCT). The sample/validation I/O is
injected so the scoring + greedy logic is pure and unit-testable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# Types unsuitable as join keys (precision/granularity) — excluded.
_NON_KEYABLE = {"float", "double", "real", "timestamp", "binary_float", "binary_double"}


@dataclass
class ColumnMeta:
    name: str
    data_type: str
    nullable: bool = True


def is_keyable(col: ColumnMeta) -> bool:
    base = col.data_type.strip().lower().split("(")[0]
    return base not in _NON_KEYABLE


def name_priority(name: str) -> int:
    """Lower is better. id < *_id/_key < code/number < others."""
    n = name.lower()
    if n == "id":
        return 0
    if n.endswith("_id") or n.endswith("_key"):
        return 1
    if "id" in n or "key" in n:
        return 2
    if "code" in n or "number" in n or "num" in n:
        return 3
    if "date" in n or "time" in n:
        return 8
    return 5


def rank_candidates(columns: list[ColumnMeta]) -> list[ColumnMeta]:
    keyable = [c for c in columns if is_keyable(c)]
    return sorted(keyable, key=lambda c: (name_priority(c.name), c.nullable, c.name))


def detect_key(
    columns: list[ColumnMeta],
    *,
    sample_distinct_ratio: Callable[[list[str]], float],
    validate_unique_full: Callable[[list[str]], bool],
    max_cols: int = 3,
    uniqueness_threshold: float = 0.99,
) -> list[str] | None:
    """Detect the *minimal* unique key, source-validated.

    Strategy (mirrors datadiff's sample-propose → confirm-on-source, but prefers the
    smallest key): rank candidates by sample distinctness (most-distinct first, id/key
    names as tie-break); if the most-distinct single column is unique, use it; otherwise
    greedily grow a composite. Full-table validation via pushdown
    `COUNT(*)==COUNT(DISTINCT key)` rejects columns that are only unique on the sample.

    `sample_distinct_ratio([cols]) -> distinct/rows on the sample`
    `validate_unique_full([cols]) -> True if COUNT(*)==COUNT(DISTINCT) on full table`
    """
    ranked = rank_candidates(columns)  # name-priority order (stable tie-break)
    # Score each single candidate by sample distinctness; keep name-priority as tie-break.
    scored: list[tuple[ColumnMeta, float, int]] = []
    for i, c in enumerate(ranked):
        try:
            ratio = sample_distinct_ratio([c.name])
        except Exception:  # noqa: BLE001 - a bad column shouldn't sink detection
            ratio = 0.0
        scored.append((c, ratio, i))
    scored.sort(key=lambda t: (-t[1], t[2]))  # most distinct first, then name priority

    # 1) Prefer a minimal single-column key: the most-distinct column that validates.
    for c, ratio, _ in scored:
        if ratio >= uniqueness_threshold and validate_unique_full([c.name]):
            return [c.name]

    # 2) Otherwise grow a composite from most-distinct-first.
    composite: list[str] = []
    for c, _, _ in scored[: max_cols * 2]:
        composite.append(c.name)
        if sample_distinct_ratio(composite) >= uniqueness_threshold and validate_unique_full(
            composite
        ):
            return composite
        if len(composite) >= max_cols:
            break
    return None
