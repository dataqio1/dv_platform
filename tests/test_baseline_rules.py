"""Historical-baseline DQ rules — the PDF acceptance criteria as tests (pure, no DB)."""

from __future__ import annotations

from datetime import date

from dvp.observability.baseline import (
    FAIL,
    LEARNING,
    PASS,
    Observation,
    evaluate,
    valid_history,
)


def _hist(values, **kw):
    return [Observation(value=v, **kw) for v in values]


# ---- Deviation: 30 days averaging 100 → 106 fails ±5%, 104 passes ----
def test_deviation_five_percent_boundary():
    history = _hist([100] * 30)
    cfg = {"aggregate": "avg", "window": {"observations": 30}, "tolerance_pct": 5, "severity": "fail"}

    fail = evaluate("deviation", 106, history, cfg)
    assert fail.status == FAIL
    assert fail.baseline_value == 100
    assert round(fail.deviation_pct, 1) == 6.0
    assert "above" in fail.message and "limit" in fail.message

    ok = evaluate("deviation", 104, history, cfg)
    assert ok.status == PASS

    assert evaluate("deviation", 105, history, cfg).status == PASS  # exactly on the limit


def test_deviation_separate_upper_lower_limits():
    history = _hist([100] * 10)
    cfg = {"aggregate": "avg", "tolerance_pct_upper": 10, "tolerance_pct_lower": 2, "severity": "fail"}
    assert evaluate("deviation", 109, history, cfg).status == PASS   # within +10%
    assert evaluate("deviation", 99, history, cfg).status == PASS    # within -2%
    assert evaluate("deviation", 97, history, cfg).status == FAIL    # below -2% → 98 floor
    assert evaluate("deviation", 112, history, cfg).status == FAIL   # above +10%


def test_deviation_direction_one_sided():
    history = _hist([100] * 10)
    # only care about drops (lower); a spike must not fail
    cfg = {"aggregate": "avg", "tolerance_pct": 5, "direction": "lower", "severity": "fail"}
    assert evaluate("deviation", 130, history, cfg).status == PASS
    assert evaluate("deviation", 90, history, cfg).status == FAIL


# ---- Cold start: < 5 valid obs → Learning; static fallback protects day one ----
def test_learning_until_min_history():
    r = evaluate("deviation", 500, _hist([100, 100, 100]), {"tolerance_pct": 5, "severity": "fail"})
    assert r.status == LEARNING
    assert "learning" in r.message.lower()


def test_static_fallback_during_learning():
    cfg = {"tolerance_pct": 5, "severity": "fail", "fallback_threshold": 200, "fallback_direction": "upper"}
    assert evaluate("deviation", 150, _hist([100]), cfg).status == PASS   # under fallback
    assert evaluate("deviation", 250, _hist([100]), cfg).status == FAIL   # over fallback


# ---- Monotonic: 1000 then 990 fails non-decreasing; after reset, 990 passes ----
def test_monotonic_non_decreasing_and_reset():
    history = [Observation(value=1000, business_date=date(2026, 10, 10))]
    cfg = {"direction": "non_decreasing", "severity": "fail"}
    assert evaluate("monotonic", 990, history, cfg).status == FAIL

    # A declared truncate/reload on 2026-10-15 restarts the comparison → nothing before it counts.
    cfg_reset = {**cfg, "reset": "2026-10-15"}
    assert evaluate("monotonic", 990, history, cfg_reset).status == PASS


def test_monotonic_tolerance_and_strict():
    history = [Observation(value=1000)]
    assert evaluate("monotonic", 999, history, {"direction": "non_decreasing", "tolerance": 5}).status == PASS
    assert evaluate("monotonic", 1000, history, {"direction": "strictly_increasing", "severity": "warn"}).status == "warn"


# ---- Growth rate: change vs previous within a min–max band ----
def test_growth_rate_band():
    history = [Observation(value=1000)]
    cfg = {"min_change": 1000, "max_change": 50000, "unit": "absolute", "severity": "warn"}
    assert evaluate("growth", 1500, history, cfg).status == "warn"    # +500 below min
    assert evaluate("growth", 2500, history, cfg).status == PASS      # +1500 in band
    assert evaluate("growth", 60000, history, cfg).status == "warn"   # +59000 above max


# ---- Baseline validity: excluded don't change it; accepted do ----
def test_excluded_observations_ignored_accepted_included():
    base = [Observation(value=100) for _ in range(5)]
    # an excluded outlier must not move the baseline
    with_excluded = base + [Observation(value=10_000, excluded=True)]
    assert valid_history(with_excluded) == valid_history(base)

    # an unaccepted Fail is excluded; once accepted it enters the baseline
    failed = Observation(value=200, status=FAIL)
    assert valid_history([*base, failed]) == valid_history(base)
    accepted = Observation(value=200, status=FAIL, accepted=True)
    assert len(valid_history([*base, accepted])) == 6


def test_unparseable_value_is_error_not_fail():
    r = evaluate("deviation", "n/a", _hist([100] * 10), {"tolerance_pct": 5, "severity": "fail"})
    assert r.status == "error"
