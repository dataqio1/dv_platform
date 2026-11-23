"""Rule → SQL pushdown evaluator.

Compiles a DQ rule into aggregate SQL, runs it via the connector (one query → one
number), and decides pass/fail from the rule config. The same code path runs on
Snowflake in production and on the in-memory SQLite `local` connector in dev/tests, so
rule logic is exercised for real without a warehouse.

Pure except for the single `connector.execute()` call, so it's easy to test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from dvp.connectors.base import TableRef
from dvp.core.errors import ValidationError

SUPPORTED = {
    "row_count",
    "not_null",
    "null_rate",
    "unique",
    "range",
    "in_set",
    "regex",
    "freshness",
    "custom_sql",
    "metric",
    "scd2_active_unique",
    "scd2_no_overlap",
}


@dataclass
class RuleEvaluation:
    metric_value: float | None
    passed: bool
    observed: dict

    def as_dict(self) -> dict:
        return {"metric_value": self.metric_value, "passed": self.passed, "observed": self.observed}


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(v: object) -> str:
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _table_sql(connector, table: TableRef) -> str:  # noqa: ANN001
    ref = getattr(connector, "sql_ref", None)
    return ref(table) if callable(ref) else table.fqn()


def _parse_ts(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def build_sql(rule: dict, table_sql: str, *, dialect: str = "sqlite") -> str:
    """Return a SQL statement that yields a single metric value as column `v`."""
    rtype = rule["type"]
    cols = rule.get("columns") or []
    cfg = rule.get("config") or {}

    if rtype == "row_count":
        return f"SELECT COUNT(*) AS v FROM {table_sql}"
    if rtype == "not_null":
        if not cols:
            raise ValidationError("not_null rule needs at least one column")
        pred = " OR ".join(f"{_q(c)} IS NULL" for c in cols)
        return f"SELECT COUNT(*) AS v FROM {table_sql} WHERE {pred}"
    if rtype == "null_rate":
        c = _q(cols[0])
        return (
            f"SELECT CAST(SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END) AS FLOAT)"
            f"/NULLIF(COUNT(*),0) AS v FROM {table_sql}"
        )
    if rtype == "unique":
        if not cols:
            raise ValidationError("unique rule needs at least one column")
        keys = ", ".join(_q(c) for c in cols)
        return f"SELECT COUNT(*) AS v FROM (SELECT 1 FROM {table_sql} GROUP BY {keys} HAVING COUNT(*)>1) d"
    if rtype == "range":
        c = _q(cols[0])
        lo, hi = cfg.get("min_value"), cfg.get("max_value")
        conds = []
        if lo is not None:
            conds.append(f"{c} < {_lit(lo)}")
        if hi is not None:
            conds.append(f"{c} > {_lit(hi)}")
        pred = " OR ".join(conds) or "1=0"
        return f"SELECT COUNT(*) AS v FROM {table_sql} WHERE {pred}"
    if rtype == "in_set":
        c = _q(cols[0])
        vals = ", ".join(_lit(v) for v in cfg.get("values", []))
        return f"SELECT COUNT(*) AS v FROM {table_sql} WHERE {c} NOT IN ({vals})"
    if rtype == "regex":
        c = _q(cols[0])
        pat = cfg.get("pattern", ".*")
        if dialect in ("snowflake", "oracle"):
            cond = f"NOT REGEXP_LIKE({c}, {_lit(pat)})"
        else:
            cond = f"NOT ({c} REGEXP {_lit(pat)})"
        return f"SELECT COUNT(*) AS v FROM {table_sql} WHERE {c} IS NOT NULL AND {cond}"
    if rtype == "freshness":
        c = _q(cols[0]) if cols else None
        if c is None:
            raise ValidationError("freshness rule needs the watermark column")
        return f"SELECT MAX({c}) AS v FROM {table_sql}"
    if rtype == "scd2_active_unique":
        keys = cfg.get("key_columns") or cols
        if not keys:
            raise ValidationError("scd2_active_unique rule needs key_columns (the business key)")
        active = cfg.get("active_sql")
        if not active:
            raise ValidationError("scd2_active_unique rule needs config.active_sql")
        klist = ", ".join(_q(c) for c in keys)
        return (
            f"SELECT COUNT(*) AS v FROM (SELECT {klist} FROM {table_sql} WHERE {active} "
            f"GROUP BY {klist} HAVING COUNT(*) > 1) d"
        )
    if rtype == "scd2_no_overlap":
        keys = cfg.get("key_columns") or cols
        sfrom = cfg.get("effective_from_column")
        sto = cfg.get("effective_to_column")
        if not (keys and sfrom and sto):
            raise ValidationError(
                "scd2_no_overlap rule needs key_columns, effective_from_column, effective_to_column"
            )
        klist = ", ".join(_q(c) for c in keys)
        return (
            f"SELECT COUNT(*) AS v FROM (SELECT {_q(sfrom)} AS s, "
            f"LAG({_q(sto)}) OVER (PARTITION BY {klist} ORDER BY {_q(sfrom)}) AS prev_end "
            f"FROM {table_sql}) w WHERE prev_end IS NOT NULL AND s < prev_end"
        )
    if rtype in ("custom_sql", "metric"):
        expr = cfg.get("expression")
        if not expr:
            raise ValidationError(f"{rtype} rule needs config.expression")
        return expr.replace("{{table}}", table_sql)
    raise ValidationError(f"unsupported rule type: {rtype}", details={"type": rtype})


_SAMPLE_LIMIT = 20


def _key_columns(rule: dict) -> list[str]:
    cfg = rule.get("config") or {}
    keys: list[str] = []
    for col in list(cfg.get("key_columns") or []) + list(rule.get("columns") or []):
        if col not in keys:
            keys.append(col)
    return keys


def _select_list(keys: list[str]) -> str:
    return ", ".join(_q(c) for c in keys) if keys else "*"


def _limit_clause(dialect: str) -> str:
    if dialect == "oracle":
        return f"FETCH FIRST {_SAMPLE_LIMIT} ROWS ONLY"
    return f"LIMIT {_SAMPLE_LIMIT}"


def _where_tail(sql: str) -> str | None:
    marker = " where "
    idx = sql.lower().rfind(marker)
    if idx < 0:
        return None
    tail = sql[idx + len(marker) :].strip().rstrip(";")
    return tail or None


def sample_sql(rule: dict, table_sql: str, metric_sql: str, *, dialect: str) -> str | None:
    """SQL that returns at most 20 failing key rows. None when the rule has no row to point at."""
    rtype = rule["type"]
    if rtype in ("row_count", "freshness", "metric"):
        return None
    lim = _limit_clause(dialect)
    cols = rule.get("columns") or []
    keys = _select_list(_key_columns(rule))
    if rtype == "not_null":
        if not cols:
            return None
        pred = " OR ".join(f"{_q(c)} IS NULL" for c in cols)
        return f"SELECT {keys} FROM {table_sql} WHERE {pred} {lim}"
    if rtype == "null_rate":
        if not cols:
            return None
        return f"SELECT {keys} FROM {table_sql} WHERE {_q(cols[0])} IS NULL {lim}"
    if rtype == "range":
        c = _q(cols[0])
        cfg = rule.get("config") or {}
        conds = []
        if cfg.get("min_value") is not None:
            conds.append(f"{c} < {_lit(cfg['min_value'])}")
        if cfg.get("max_value") is not None:
            conds.append(f"{c} > {_lit(cfg['max_value'])}")
        if not conds:
            return None
        return f"SELECT {keys} FROM {table_sql} WHERE {' OR '.join(conds)} {lim}"
    if rtype == "in_set":
        c = _q(cols[0])
        vals = ", ".join(_lit(v) for v in (rule.get("config") or {}).get("values", []))
        if not vals:
            return None
        return f"SELECT {keys} FROM {table_sql} WHERE {c} NOT IN ({vals}) {lim}"
    if rtype == "regex":
        c = _q(cols[0])
        pat = (rule.get("config") or {}).get("pattern", ".*")
        if dialect in ("snowflake", "oracle"):
            cond = f"NOT REGEXP_LIKE({c}, {_lit(pat)})"
        else:
            cond = f"NOT ({c} REGEXP {_lit(pat)})"
        return f"SELECT {keys} FROM {table_sql} WHERE {c} IS NOT NULL AND {cond} {lim}"
    if rtype == "unique":
        if not cols:
            return None
        grouped = ", ".join(_q(c) for c in cols)
        return (
            f"SELECT {grouped}, COUNT(*) AS duplicate_count FROM {table_sql} "
            f"GROUP BY {grouped} HAVING COUNT(*) > 1 {lim}"
        )
    if rtype == "custom_sql":
        where = _where_tail(metric_sql)
        if not where:
            return None
        return f"SELECT {keys} FROM {table_sql} WHERE {where} {lim}"
    if rtype == "scd2_active_unique":
        cfg = rule.get("config") or {}
        kcols = cfg.get("key_columns") or cols
        active = cfg.get("active_sql")
        if not (kcols and active):
            return None
        klist = ", ".join(_q(c) for c in kcols)
        return (
            f"SELECT {klist}, COUNT(*) AS active_versions FROM {table_sql} WHERE {active} "
            f"GROUP BY {klist} HAVING COUNT(*) > 1 {lim}"
        )
    if rtype == "scd2_no_overlap":
        cfg = rule.get("config") or {}
        kcols = cfg.get("key_columns") or cols
        sfrom, sto = cfg.get("effective_from_column"), cfg.get("effective_to_column")
        if not (kcols and sfrom and sto):
            return None
        klist = ", ".join(_q(c) for c in kcols)
        return (
            f"SELECT {klist}, s AS effective_from, prev_end AS prior_effective_to FROM "
            f"(SELECT {klist}, {_q(sfrom)} AS s, "
            f"LAG({_q(sto)}) OVER (PARTITION BY {klist} ORDER BY {_q(sfrom)}) AS prev_end "
            f"FROM {table_sql}) w WHERE prev_end IS NOT NULL AND s < prev_end {lim}"
        )
    return None


def _jsonable(value: object) -> object:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _sample_rows(connector, sql: str) -> list[dict]:  # noqa: ANN001
    res = connector.execute(sql)
    rows = []
    for row in res.rows[:_SAMPLE_LIMIT]:
        rows.append({col: _jsonable(val) for col, val in zip(res.columns, row, strict=False)})
    return rows


def _decide(rule: dict, value: float | None) -> bool:
    rtype = rule["type"]
    cfg = rule.get("config") or {}
    if value is None:
        return True
    if rtype == "row_count":
        return value >= float(cfg.get("min_rows", 1))
    if rtype == "null_rate":
        return value <= float(cfg.get("max_rate", 0.0))
    if rtype in (
        "not_null", "unique", "range", "in_set", "regex",
        "scd2_active_unique", "scd2_no_overlap",
    ):
        return value == 0
    if rtype in ("custom_sql", "metric"):
        thr = cfg.get("threshold")
        if thr is None:
            return True  # a tracked metric with no threshold never "fails"
        cmp = cfg.get("comparator", "le")
        return {
            "le": value <= thr,
            "lt": value < thr,
            "ge": value >= thr,
            "gt": value > thr,
            "eq": value == thr,
            "ne": value != thr,
        }.get(cmp, value <= thr)
    return True


def evaluate(
    connector,  # noqa: ANN001
    table: TableRef,
    rule: dict,
    *,
    now: datetime | None = None,
    dialect: str | None = None,
) -> RuleEvaluation:
    if rule["type"] not in SUPPORTED:
        raise ValidationError(f"unsupported rule type: {rule['type']}")
    # The source owns its dialect — the engine stays source-agnostic.
    dialect = dialect or getattr(connector, "dialect", "sqlite")
    table_sql = _table_sql(connector, table)
    cfg = rule.get("config") or {}

    # Warehouse-native freshness (no load/watermark column, e.g. curated/published tables): read the
    # table's last-altered time from the warehouse catalog instead of MAX(column). Handled before
    # build_sql, which would otherwise require a column.
    if rule["type"] == "freshness" and cfg.get("source") == "warehouse":
        now = now or datetime.now(UTC)
        la = getattr(connector, "last_altered", None)
        last = _parse_ts(la(table) if callable(la) else None)
        if last is None:
            return RuleEvaluation(
                metric_value=None, passed=False,
                observed={"reason": "warehouse last-altered unavailable", "source": "warehouse"},
            )
        lag = max(0.0, (now - last).total_seconds())
        max_age = float(cfg.get("max_age_hours", 24))
        return RuleEvaluation(
            metric_value=lag, passed=lag <= max_age * 3600,
            observed={"last_load_ts": last.isoformat(), "max_age_hours": max_age, "source": "warehouse"},
        )

    sql = build_sql(rule, table_sql, dialect=dialect)
    res = connector.execute(sql)
    raw = res.rows[0][0] if res.rows else None

    if rule["type"] == "freshness":
        now = now or datetime.now(UTC)
        last = _parse_ts(raw)
        if last is None:
            return RuleEvaluation(
                metric_value=None, passed=False, observed={"reason": "no watermark value"}
            )
        lag = max(0.0, (now - last).total_seconds())
        max_age = float(cfg.get("max_age_hours", 24))
        return RuleEvaluation(
            metric_value=lag,
            passed=lag <= max_age * 3600,
            observed={"last_load_ts": last.isoformat(), "max_age_hours": max_age},
        )

    value = float(raw) if raw is not None else None
    passed = _decide(rule, value)
    observed: dict = {"sql": sql}
    if not passed:
        probe = sample_sql(rule, table_sql, sql, dialect=dialect)
        if probe:
            observed["sample_sql"] = probe
            try:
                observed["failing_keys"] = _sample_rows(connector, probe)
            except Exception as exc:  # noqa: BLE001 - the metric already decided the failure
                observed["sample_error"] = str(exc)
    return RuleEvaluation(metric_value=value, passed=passed, observed=observed)
