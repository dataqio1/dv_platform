"""Deadline-mode freshness + inferring a freshness policy from past loads (no mocks)."""
from __future__ import annotations

from datetime import UTC, datetime

from dvp.observability import alertpolicy as ap

DL = {"freshness": {"mode": "deadline", "deadline_time": "06:00", "grace_minutes": 30, "cadence_hours": 24}}


def _dt(d, h, m):
    return datetime(2026, 10, d, h, m, tzinfo=UTC).timestamp()


def _status(now, last_load, policy=DL):
    return ap.freshness_deadline_status(last_load, now, policy)


def test_met_deadline_is_healthy():
    st, detail = _status(_dt(20, 7, 0), _dt(20, 5, 55))  # today's load arrived before 06:00
    assert st == "healthy" and "Met deadline" in detail


def test_within_grace_is_healthy():
    st, _ = _status(_dt(20, 6, 15), _dt(19, 5, 55))  # before 06:30 cutoff; still on track
    assert st == "healthy"


def test_overdue_warns():
    st, detail = _status(_dt(20, 7, 0), _dt(19, 5, 55))  # 30m past cutoff, today missing
    assert st == "warning" and "Overdue" in detail


def test_badly_overdue_is_critical():
    st, _ = _status(_dt(20, 8, 0), _dt(19, 5, 55))  # 90m past cutoff → escalate
    assert st == "critical"


def test_no_deadline_time_is_unknown():
    st, _ = ap.freshness_deadline_status(_dt(20, 7, 0), _dt(20, 5, 55),
                                         {"freshness": {"mode": "deadline"}})
    assert st == "unknown"


def test_freshness_status_dispatches_on_mode():
    # deadline mode honored by the unified entry point
    st, _ = ap.freshness_status(3600, _dt(20, 5, 55), _dt(20, 7, 0), DL)
    assert st == "healthy"
    # staleness mode: 100h lag → critical (default severe 72h)
    st2, _ = ap.freshness_status(100 * 3600, None, _dt(20, 7, 0), None)
    assert st2 == "critical"


# ----------------------------- inference -----------------------------
def test_infer_daily_suggests_deadline():
    loads = [_dt(10 + i, 5, 50) for i in range(10)]  # ~05:50 every day for 10 days
    s = ap.infer_freshness(loads)
    assert s is not None and s["mode"] == "deadline"
    assert s["cadence_hours"] == 24
    assert s["deadline_time"] >= "05:50"  # deadline at/after the typical completion
    assert s["loads_observed"] == 10 and s["confidence"] >= 0.9


def test_infer_six_hourly_suggests_staleness():
    base = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)
    loads = [(base.timestamp() + i * 6 * 3600) for i in range(12)]  # every 6h
    s = ap.infer_freshness(loads)
    assert s is not None and s["mode"] == "staleness"
    assert s["cadence_hours"] == 6
    assert 7 <= s["warn_after_hours"] <= 8 and 11 <= s["severe_after_hours"] <= 13


def test_infer_insufficient_history_returns_none():
    assert ap.infer_freshness([_dt(10, 6, 0), _dt(11, 6, 0)]) is None
    assert ap.infer_freshness([]) is None
