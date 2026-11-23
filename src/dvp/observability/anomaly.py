"""Robust rolling-baseline anomaly detection (pure).

Evaluates the latest point in a metric series against a baseline learned from the
trailing window. Uses the **median + MAD** (median absolute deviation) rather than
mean/stddev so a single spike doesn't poison the baseline, and returns an *expected
band* [lo, hi] so the UI can overlay it and every alert ships with evidence.

Deliberately conservative on thin history: with fewer than `min_points` prior
observations it reports "no anomaly / insufficient history" instead of firing — the #1
complaint about ML observability tools is noise on young/early-stage data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


@dataclass
class AnomalyResult:
    anomaly: bool
    value: float | None
    median: float | None
    lo: float | None
    hi: float | None
    score: float  # robust z-score of the latest point
    direction: str  # "spike" | "drop" | "none"
    reason: str
    missing_scans: int | None = None
    weekday_name: str | None = None

    def as_dict(self) -> dict:
        return {
            "anomaly": self.anomaly,
            "value": self.value,
            "median": self.median,
            "lo": self.lo,
            "hi": self.hi,
            "score": round(self.score, 2),
            "direction": self.direction,
            "reason": self.reason,
            "missing_scans": self.missing_scans,
            "weekday_name": self.weekday_name,
        }


def detect_anomaly(
    series: list[float],
    *,
    min_points: int = 5,
    window: int = 30,
    k: float = 3.5,
    direction: str = "both",  # both | drop | spike
) -> AnomalyResult:
    """Evaluate the last value of `series` (chronological) against the trailing baseline."""
    vals = [float(v) for v in series if v is not None]
    if len(vals) < min_points + 1:
        return AnomalyResult(
            False, vals[-1] if vals else None, None, None, None, 0.0, "none", "insufficient history"
        )
    value = vals[-1]
    prior = vals[-(window + 1) : -1]  # trailing window, excluding the point under test
    med = _median(prior)
    mad = _median([abs(x - med) for x in prior])
    scaled = 1.4826 * mad  # ~ stddev for normal data
    if scaled == 0:
        # Flat history: fall back to mean absolute deviation, then a tiny relative floor.
        mean_abs = sum(abs(x - med) for x in prior) / len(prior)
        scaled = mean_abs or (abs(med) * 0.02) or 1.0
    lo, hi = med - k * scaled, med + k * scaled
    score = (value - med) / scaled
    hit_spike = value > hi
    hit_drop = value < lo
    if direction == "drop":
        anomaly = hit_drop
    elif direction == "spike":
        anomaly = hit_spike
    else:
        anomaly = hit_spike or hit_drop
    dir_label = "spike" if hit_spike else "drop" if hit_drop else "none"
    reason = (
        f"{dir_label} {abs(score):.1f}σ vs baseline {med:,.0f} (band {lo:,.0f}–{hi:,.0f})"
        if anomaly
        else "within expected band"
    )
    return AnomalyResult(
        anomaly, value, med, lo, hi, score, dir_label if anomaly else "none", reason
    )


def detect_weekday_volume(
    points: list[tuple[datetime, float]],
    *,
    min_same_weekday: int = 3,
) -> AnomalyResult:
    """Score the latest row count against earlier observations on the same weekday.

    Fewer than three prior same-weekdays is thin history: no band, no alert.
    """
    clean = [(ts, float(v)) for ts, v in points if v is not None]
    if not clean:
        return AnomalyResult(False, None, None, None, None, 0.0, "none", "not enough history to alert")
    ts, value = clean[-1]
    day = _WEEKDAYS[ts.weekday()]
    prior = [v for t, v in clean[:-1] if t.weekday() == ts.weekday()]
    if len(prior) < min_same_weekday:
        return AnomalyResult(
            False,
            value,
            None,
            None,
            None,
            0.0,
            "none",
            "not enough history to alert",
            min_same_weekday - len(prior),
            day,
        )
    res = detect_anomaly(prior + [value], min_points=min_same_weekday, direction="both")
    if res.anomaly:
        res.reason = (
            f"{res.direction} {abs(res.score):.1f}σ vs {day} baseline {res.median:,.0f} "
            f"(band {res.lo:,.0f}–{res.hi:,.0f})"
        )
    elif res.lo is not None:
        res.reason = f"within {day} band"
    return res
