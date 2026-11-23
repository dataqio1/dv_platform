"""Profile a SAMPLE of rows into a TableProfile (for rule auto-generation) and dry-run rule specs
against that same sample — both pure (rows in → result out) so they are trivially unit-testable
and work for any source (table / SQL / file-on-S3) since the caller supplies the sampled rows.

Used by the 'generate DQ rules from the latest batch / N rows' and 'test rules on the sample'
flows. Nothing here persists.
"""

from __future__ import annotations

import re

from dvp.observability.autogen import ColumnProfile, TableProfile
from dvp.testing_engine.typemap import canonicalize

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _hashable(v: object) -> object:
    return v if v is None or isinstance(v, (str, int, float, bool)) else str(v)


def _as_number(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def build_profile(table: str, cols: list, rows: list[dict], *, key: list[str] | None = None,
                  load_ts_column: str | None = None) -> TableProfile:  # noqa: ANN001
    """Compute a TableProfile from a sample. `cols` are ColumnDef-like (``.name``/``.data_type``)."""
    n = len(rows)
    profiles: list[ColumnProfile] = []
    for c in cols:
        name = c.name
        vals = [r.get(name) for r in rows]
        non_null = [v for v in vals if v is not None]
        null_rate = round((n - len(non_null)) / n, 6) if n else 0.0
        distinct = len({_hashable(v) for v in non_null})
        distinct_ratio = round(distinct / n, 6) if n else 0.0
        canon = canonicalize(c.data_type).value
        min_v = max_v = None
        if canon == "NUMERIC" and non_null:
            nums = [x for x in (_as_number(v) for v in non_null) if x is not None]
            if nums:
                min_v, max_v = min(nums), max(nums)
        looks_email = bool(
            canon == "STRING" and non_null
            and sum(1 for v in non_null if isinstance(v, str) and _EMAIL_RE.match(v)) >= 0.9 * len(non_null)
        )
        profiles.append(ColumnProfile(
            name=name, data_type=canon, null_rate=null_rate, distinct_ratio=distinct_ratio,
            min_value=min_v, max_value=max_v,
            is_key_candidate=distinct_ratio >= 0.99 and null_rate == 0.0,
            looks_like_email=looks_email,
        ))
    return TableProfile(table=table, row_count=n, columns=profiles,
                        load_ts_column=load_ts_column, detected_key=list(key or []))


def _small(row: dict, cols: list[str] | None) -> dict:
    """A trimmed row for the UI — just the columns the rule touches (plus a couple of context cols)."""
    if not cols:
        return dict(list(row.items())[:6])
    return {c: row.get(c) for c in cols}


def evaluate_rule_on_rows(rule: dict, rows: list[dict]) -> dict:
    """Dry-run ONE rule spec against sampled rows (pure). Returns pass/fail + a few failing examples.
    Time-based rules (freshness) and custom_sql aren't evaluable on an in-memory sample and are
    reported as not-applicable rather than silently passing."""
    rtype = rule.get("type")
    cols = rule.get("columns") or []
    cfg = rule.get("config") or {}
    col = cols[0] if cols else None
    name = rule.get("name")
    failing: list[dict] = []

    def result(passed: bool, measured, applicable: bool = True, note: str | None = None) -> dict:
        out = {"name": name, "type": rtype, "applicable": applicable, "passed": passed,
               "measured": measured, "failing": [_small(r, cols) for r in failing[:20]]}
        if note:
            out["note"] = note
        return out

    if rtype == "not_null":
        failing = [r for r in rows if r.get(col) is None]
        return result(not failing, len(failing))
    if rtype == "null_rate":
        nulls = sum(1 for r in rows if r.get(col) is None)
        rate = round(nulls / len(rows), 6) if rows else 0.0
        failing = [r for r in rows if r.get(col) is None]
        return result(rate <= float(cfg.get("max_rate", 0)), rate)
    if rtype == "row_count":
        return result(len(rows) >= int(cfg.get("min_rows", 1)), len(rows))
    if rtype == "unique":
        seen: set = set()
        for r in rows:
            k = tuple(_hashable(r.get(c)) for c in cols)
            if k in seen:
                failing.append(r)
            seen.add(k)
        return result(not failing, len(failing))
    if rtype == "range":
        lo, hi = cfg.get("min_value"), cfg.get("max_value")
        for r in rows:
            v = _as_number(r.get(col))
            if v is None:
                continue
            if (lo is not None and v < float(lo)) or (hi is not None and v > float(hi)):
                failing.append(r)
        return result(not failing, len(failing))
    if rtype == "in_set":
        allowed = set(cfg.get("values") or [])
        failing = [r for r in rows if r.get(col) is not None and r.get(col) not in allowed]
        return result(not failing, len(failing))
    if rtype == "regex":
        pat = re.compile(cfg.get("pattern", ".*"))
        failing = [r for r in rows if isinstance(r.get(col), str) and not pat.match(r.get(col))]
        return result(not failing, len(failing))
    return result(True, None, applicable=False,
                  note="not evaluable on an in-memory sample — run the monitor to measure it")
