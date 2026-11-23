"""The four pillars of data observability (pure logic).

Pillars: FRESHNESS, VOLUME, SCHEMA, QUALITY (quality/distribution). Each table page
shows a card per pillar with a health status and headline, plus history over time and
column-level drill-down (see the per-table observability screenshot).

Metrics are persisted in the `metric` time-series table under a stable naming
convention so the same rows feed both the current-status cards and the history charts.
This module is pure: it maps metric values → pillar status; the service layer reads the
rows and calls these helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum


class Pillar(StrEnum):
    FRESHNESS = "freshness"
    VOLUME = "volume"
    SCHEMA = "schema"
    QUALITY = "quality"


class Health(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


# Metric names (table-level unless noted). Column-level metrics carry tags.column.
M_FRESHNESS_LAG = "freshness.lag_seconds"
M_FRESHNESS_LAST_LOAD = "freshness.last_load_epoch"
M_VOLUME_ROWS = "volume.row_count"
M_SCHEMA_COLS = "schema.column_count"
M_SCHEMA_DRIFT = "schema.drift_count"
M_QUALITY_SCORE = "quality.score"
M_COL_NULL_RATE = "quality.null_rate"
M_COL_DISTINCT = "quality.distinct_ratio"

# The primary history metric for each pillar.
PILLAR_PRIMARY_METRIC = {
    Pillar.FRESHNESS: M_FRESHNESS_LAG,
    Pillar.VOLUME: M_VOLUME_ROWS,
    Pillar.SCHEMA: M_SCHEMA_COLS,
    Pillar.QUALITY: M_QUALITY_SCORE,
}

def worst(statuses: list[Health]) -> Health:
    """Critical and warning always surface. Healthy requires every pillar to be measured.

    An unmeasured pillar is not a pass. Calling a half-observed table healthy is how
    observability tools lose trust.
    """
    if not statuses:
        return Health.UNKNOWN
    if Health.CRITICAL in statuses:
        return Health.CRITICAL
    if Health.WARNING in statuses:
        return Health.WARNING
    if any(s != Health.HEALTHY for s in statuses):
        return Health.UNKNOWN
    return Health.HEALTHY


def _human_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def freshness_card(
    lag_seconds: float | None,
    last_load_epoch: float | None,
    config: dict,
    reason: str | None = None,
) -> dict:
    if lag_seconds is None:
        card = _unknown_card(Pillar.FRESHNESS)
        if reason:
            card["headline"] = reason
            card["configured"] = True
        return card
    warn_h = float(config.get("warn_hours", 24))
    crit_h = float(config.get("crit_hours", 72))
    if lag_seconds <= warn_h * 3600:
        status = Health.HEALTHY
    elif lag_seconds <= crit_h * 3600:
        status = Health.WARNING
    else:
        status = Health.CRITICAL
    headline = "Last load unknown"
    if last_load_epoch is not None:
        dt = datetime.fromtimestamp(last_load_epoch, tz=UTC)
        headline = f"Last load {dt.strftime('%b %d %Y, %H:%M')} UTC"
    return {
        "pillar": Pillar.FRESHNESS.value,
        "status": status.value,
        "headline": headline,
        "detail": _human_age(lag_seconds),
        "value": lag_seconds,
        "last_load_at": _iso_from_epoch(last_load_epoch) if last_load_epoch is not None else None,
        "configured": True,
    }


def volume_card(
    row_count: float | None,
    prior: float | None,
    config: dict,
    *,
    weekday: dict | None = None,
) -> dict:
    if row_count is None:
        return _unknown_card(Pillar.VOLUME)
    if weekday is not None:
        reason = weekday.get("reason") or ""
        if reason == "not enough history to alert":
            missing = weekday.get("missing_scans")
            day = weekday.get("weekday_name")
            if missing and day:
                noun = "scan" if int(missing) == 1 else "scans"
                detail = f"need {int(missing)} more {day} {noun} before volume can alert"
            else:
                detail = reason
            status = Health.HEALTHY
        elif weekday.get("anomaly"):
            score = abs(float(weekday.get("score") or 0))
            status = Health.CRITICAL if score >= 6 else Health.WARNING
            detail = reason
        else:
            status, detail = Health.HEALTHY, reason or "within expected band"
        return {
            "pillar": Pillar.VOLUME.value,
            "status": status.value,
            "headline": f"{int(row_count):,} rows",
            "detail": detail,
            "value": row_count,
            "configured": True,
        }
    warn_drop = float(config.get("warn_drop_pct", 20)) / 100.0
    crit_drop = float(config.get("crit_drop_pct", 50)) / 100.0
    status = Health.HEALTHY
    detail = "within expected range"
    if prior and prior > 0:
        change = (row_count - prior) / prior
        pct = round(change * 100, 1)
        detail = f"{'+' if pct >= 0 else ''}{pct}% vs previous run"
        drop = -change
        if drop >= crit_drop:
            status = Health.CRITICAL
        elif drop >= warn_drop:
            status = Health.WARNING
    return {
        "pillar": Pillar.VOLUME.value,
        "status": status.value,
        "headline": f"{int(row_count):,} rows",
        "detail": detail,
        "value": row_count,
        "configured": True,
    }


def column_types(columns: list | None) -> dict[str, str | None]:
    """Map column name to type. Accepts plain names or ``{name, data_type}``."""
    out: dict[str, str | None] = {}
    for col in columns or []:
        if isinstance(col, str):
            out[col] = None
        elif isinstance(col, dict) and col.get("name"):
            out[str(col["name"])] = col.get("data_type") or col.get("type")
    return out


def diff_columns(prior: list | None, current: list | None) -> dict:
    """Added, removed, and type-changed columns between two snapshots."""
    before = column_types(prior)
    after = column_types(current)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    type_changed = []
    for name in sorted(set(before) & set(after)):
        old, new = before[name], after[name]
        if old and new and str(old).upper() != str(new).upper():
            type_changed.append({"column": name, "from": str(old), "to": str(new)})
    return {"added": added, "removed": removed, "type_changed": type_changed}


def describe_schema_changes(changes: dict | None, table: str | None = None) -> str:
    if not changes:
        return ""
    where = f" on {table}" if table else ""
    bits: list[str] = []
    for name in changes.get("added") or []:
        bits.append(f"{name} added{where}")
    for name in changes.get("removed") or []:
        bits.append(f"{name} removed{where}")
    for change in changes.get("type_changed") or []:
        bits.append(
            f"{change['column']} changed from {change['from']} to {change['to']}{where}"
        )
    return "; ".join(bits)


def change_count(changes: dict | None) -> int:
    if not changes:
        return 0
    return (
        len(changes.get("added") or [])
        + len(changes.get("removed") or [])
        + len(changes.get("type_changed") or [])
    )


def type_gap_detail(columns: list | None) -> str | None:
    """A column list with no types cannot catch a type change. None means no list was stored."""
    if not columns:
        return None
    missing: list[str] = []
    named = 0
    for col in columns:
        if isinstance(col, str):
            named += 1
            missing.append(col)
            continue
        if not isinstance(col, dict):
            continue
        name = str(col.get("name") or col.get("column") or "")
        if not name:
            continue
        named += 1
        if not (col.get("data_type") or col.get("type")):
            missing.append(name)
    if not missing or named == 0:
        return None
    if len(missing) == named:
        return "Types were not recorded, so a type change would be missed"
    if len(missing) == 1:
        return f"{missing[0]} has no type, so a type change there would be missed"
    shown = ", ".join(missing[:3])
    if len(missing) > 3:
        shown += f" and {len(missing) - 3} more"
    return f"{shown} have no type, so a type change there would be missed"


def schema_card(
    column_count: float | None,
    drift_count: float | None,
    config: dict,
    changes: dict | None = None,
    columns: list | None = None,
) -> dict:
    if column_count is None:
        return _unknown_card(Pillar.SCHEMA)
    drift = int(drift_count or 0)
    named = describe_schema_changes(changes)
    if drift == 0:
        gap = type_gap_detail(columns)
        if gap:
            status, detail = Health.UNKNOWN, gap
        else:
            status, detail = Health.HEALTHY, "no changes since last run"
    else:
        status = Health.WARNING
        detail = named or f"{drift} schema change(s) since last run"
    return {
        "pillar": Pillar.SCHEMA.value,
        "status": status.value,
        "headline": f"{int(column_count)} columns",
        "detail": detail,
        "value": column_count,
        "drift_count": drift,
        "configured": True,
    }


def quality_card(score: float | None, config: dict, reason: str | None = None) -> dict:
    if score is None:
        card = _unknown_card(Pillar.QUALITY)
        if reason:
            card["headline"] = reason
            card["configured"] = True
        return card
    warn = float(config.get("warn_score", 95))
    crit = float(config.get("crit_score", 80))
    if score >= warn:
        status = Health.HEALTHY
    elif score >= crit:
        status = Health.WARNING
    else:
        status = Health.CRITICAL
    return {
        "pillar": Pillar.QUALITY.value,
        "status": status.value,
        "headline": f"{score:.0f}/100",
        "detail": "quality & distribution score",
        "value": score,
        "configured": True,
    }


def _unknown_card(pillar: Pillar) -> dict:
    return {
        "pillar": pillar.value,
        "status": Health.UNKNOWN.value,
        "headline": "Not configured yet",
        "detail": None,
        "value": None,
        "configured": False,
    }


def overall_score(cards: list[dict]) -> int:
    """A 0..100 roll-up. Unknown pillars score 0, so 100 means every pillar was measured and healthy."""
    weights = {
        Health.HEALTHY.value: 100,
        Health.WARNING.value: 60,
        Health.CRITICAL.value: 20,
        Health.UNKNOWN.value: 0,
    }
    if not cards:
        return 0
    vals = [weights[c["status"]] for c in cards]
    return round(sum(vals) / len(vals))


# Load-time names, best first. A warehouse prefix such as O_ is stripped once.
_WATERMARK_NAMES = (
    "load_ts",
    "ingested_at",
    "loaded_at",
    "inserted_at",
    "created_at",
    "updated_at",
    "modified_at",
    "event_time",
    "orderdate",
    "order_date",
)


def suggest_watermark(columns: list[dict]) -> dict | None:
    """A likely load-time column, or None. The caller confirms it before a scan uses it."""
    best: tuple[int, str, str] | None = None
    for col in columns:
        # Column entries may be dicts ({name, data_type}) or plain name strings.
        if isinstance(col, str):
            raw, dtype = col, ""
        elif isinstance(col, dict):
            raw = str(col.get("name") or col.get("column") or "")
            dtype = str(col.get("data_type") or "").upper()
        else:
            continue
        if not raw:
            continue
        low = raw.lower()
        forms = [low]
        if len(low) > 2 and low[0].isalpha() and low[1] == "_":
            forms.append(low[2:])
        rank = next((i for form in forms for i, name in enumerate(_WATERMARK_NAMES) if form == name), None)
        if rank is None:
            continue
        if "DATE" in dtype and "TIME" not in dtype:
            wtype = "date"
        elif low.endswith("date"):
            wtype = "date"
        else:
            wtype = "timestamp"
        if best is None or rank < best[0]:
            best = (rank, raw, wtype)
    if best is None:
        return None
    return {"column": best[1], "type": best[2]}
