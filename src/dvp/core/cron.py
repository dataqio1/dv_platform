"""Minimal 5-field cron matcher (minute hour day-of-month month day-of-week).

Supports `*`, `*/n`, `a-b`, `a,b,c`, and single values — enough for the digest cadences a
report scheduler needs (hourly / daily-at / weekly-on). Day-of-week is 0-6 with 0 = Sunday
(7 also accepted for Sunday), matching standard cron.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def _field(expr: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in expr.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        values.update(range(start, end + 1, step))
    return values


def is_valid(expr: str) -> bool:
    """True if `expr` is a parseable 5-field cron expression (fields in range)."""
    fields = expr.split()
    if len(fields) != 5:
        return False
    bounds = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
    try:
        for part, (lo, hi) in zip(fields, bounds, strict=True):
            vals = _field(part, lo, hi)
            if not vals or any(v < lo or v > hi for v in vals):
                return False
    except (ValueError, TypeError):
        return False
    return True


def is_due(expr: str, dt: datetime) -> bool:
    """True if `dt` (minute precision) matches the cron expression."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron needs 5 fields, got {len(fields)}: {expr!r}")
    minute, hour, dom, month, dow = fields
    dow_set = {d % 7 for d in _field(dow, 0, 7)}  # normalize 7 → 0 (Sunday)
    return (
        dt.minute in _field(minute, 0, 59)
        and dt.hour in _field(hour, 0, 23)
        and dt.day in _field(dom, 1, 31)
        and dt.month in _field(month, 1, 12)
        and (dt.isoweekday() % 7) in dow_set  # isoweekday: Mon=1..Sun=7 → Sun=0
    )


def previous_due(expr: str, dt: datetime) -> datetime | None:
    """Most recent minute at or before `dt` that matches `expr`, within 8 days."""
    cursor = dt.replace(second=0, microsecond=0)
    for _ in range(8 * 24 * 60):
        if is_due(expr, cursor):
            return cursor
        cursor -= timedelta(minutes=1)
    return None


def next_due(expr: str, dt: datetime) -> datetime | None:
    """Next minute strictly after `dt` that matches `expr`, within 8 days."""
    cursor = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(8 * 24 * 60):
        if is_due(expr, cursor):
            return cursor
        cursor += timedelta(minutes=1)
    return None
