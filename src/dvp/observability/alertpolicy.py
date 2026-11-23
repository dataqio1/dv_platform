"""Per-monitor alert policy — freshness cadence + grace, and per-pillar severity thresholds.

One policy per monitor applies identically to every table *and* file in that monitor, so
alerting behaves the same whether the source is a warehouse table or an S3 file.

Policy shape (all keys optional; missing keys fall back to the defaults below, which
reproduce the engine's historical behavior so a monitor with no policy is unchanged)::

    {
      "freshness": {
        "source": "auto",            # auto | watermark | file_arrival
        "cadence_hours": 24,         # how often new data is expected (informational)
        "warn_after_hours": 24,      # warn when newest data is older than this
        "severe_after_hours": 72     # escalate to critical past this
      },
      "quality":  {"warn_below": 80, "severe_below": 60},   # table quality score 0-100
      "volume":   {"warn_pct": 20, "severe_pct": 50}        # % deviation from baseline (optional)
    }

`freshness.source`:
  - ``watermark``     — freshness is MAX(watermark_column), as for warehouse tables.
  - ``file_arrival``  — freshness is the newest file's last-modified time (S3/local files).
  - ``auto``          — watermark when a watermark column is set, else file arrival when the
                        connector can report it. This is the right default for most monitors.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta

WARNING = "warning"
CRITICAL = "critical"

# Defaults chosen to reproduce the pre-policy engine behavior exactly:
#   freshness warned at 24h / critical at 72h; quality warned <80 / critical <60.
#
# `mode` picks how freshness is judged:
#   - "staleness" (default): warn/critical when the newest data is older than N hours (relative).
#   - "deadline":  the data must arrive by a wall-clock time each cycle (e.g. "by 06:00 UTC daily,
#                  grace 30m"). `deadline_time` is "HH:MM" UTC; `grace_minutes` is the allowance.
DEFAULT_FRESHNESS = {
    "source": "auto",
    "mode": "staleness",
    "cadence_hours": 24.0,
    "warn_after_hours": 24.0,
    "severe_after_hours": 72.0,
    "deadline_time": None,     # "HH:MM" UTC when mode == "deadline"
    "grace_minutes": 30.0,
}
DEFAULT_QUALITY = {"warn_below": 80.0, "severe_below": 60.0}
# Volume keeps the historical anomaly z-score classifier unless explicit pct thresholds are set.
DEFAULT_VOLUME: dict = {"warn_pct": None, "severe_pct": None}
# Schema: severity to apply per change kind. Distribution: drift method + thresholds.
DEFAULT_SCHEMA = {"added": "error", "removed": "error", "type_changed": "error"}
DEFAULT_DISTRIBUTION = {"method": "PSI", "warn": 0.1, "error": 0.25}

AUTO = "auto"
WATERMARK = "watermark"
FILE_ARRIVAL = "file_arrival"

_SEV_RANK = {"ignore": 0, "warn": 1, "warning": 1, "error": 2, "critical": 2}


def resolve(policy: dict | None) -> dict:
    """Merge a (possibly partial / None) policy with the defaults into a full policy."""
    p = dict(policy or {})
    return {
        "freshness": {**DEFAULT_FRESHNESS, **(p.get("freshness") or {})},
        "quality": {**DEFAULT_QUALITY, **(p.get("quality") or {})},
        "volume": {**DEFAULT_VOLUME, **(p.get("volume") or {})},
        "schema": {**DEFAULT_SCHEMA, **(p.get("schema") or {})},
        "distribution": {**DEFAULT_DISTRIBUTION, **(p.get("distribution") or {})},
    }


def schema_severity(policy: dict | None, changes: dict | None) -> str | None:
    """Worst configured severity across the schema change kinds actually present.

    `changes` is the diff_columns output (added/removed/type_changed lists). Returns
    'warning'/'critical'/None so the schema pillar can honor the per-kind policy."""
    sch = {**DEFAULT_SCHEMA, **((policy or {}).get("schema") or {})}
    present = []
    for kind in ("added", "removed", "type_changed"):
        if (changes or {}).get(kind):
            present.append(sch.get(kind, "error"))
    if not present:
        return None
    worst = max(present, key=lambda s: _SEV_RANK.get(s, 2))
    if _SEV_RANK.get(worst, 2) == 0:
        return None  # ignore
    return "critical" if _SEV_RANK.get(worst, 2) >= 2 else "warning"


def distribution_severity(psi: float | None, policy: dict | None) -> str | None:
    """Map a drift score to severity via the distribution thresholds."""
    if psi is None:
        return None
    d = {**DEFAULT_DISTRIBUTION, **((policy or {}).get("distribution") or {})}
    if psi >= float(d["error"]):
        return "critical"
    if psi >= float(d["warn"]):
        return "warning"
    return None


def freshness_source(policy: dict | None) -> str:
    return str(resolve(policy)["freshness"].get("source") or AUTO).lower()


def _f(value: object, fallback: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def freshness_severity(lag_seconds: float, policy: dict | None) -> str | None:
    """Severity for a freshness lag, or ``None`` when the data is within its arrival SLA."""
    f = resolve(policy)["freshness"]
    warn = _f(f.get("warn_after_hours"), 24.0) * 3600
    severe = _f(f.get("severe_after_hours"), 72.0) * 3600
    if lag_seconds > severe:
        return CRITICAL
    if lag_seconds > warn:
        return WARNING
    return None


def warn_after_seconds(policy: dict | None) -> float:
    return _f(resolve(policy)["freshness"].get("warn_after_hours"), 24.0) * 3600


# ----------------------------- deadline-mode freshness ("by 06:00 daily") -----------------------
def _parse_hhmm(value: object) -> tuple[int, int] | None:
    try:
        h, m = str(value).split(":")
        h, m = int(h), int(m)
        return (h, m) if 0 <= h < 24 and 0 <= m < 60 else None
    except (ValueError, AttributeError):
        return None


def freshness_deadline_status(
    last_load_epoch: float | None, now_epoch: float, policy: dict | None
) -> tuple[str, str]:
    """Deadline-mode freshness: the data must arrive by a wall-clock time each cycle.

    Returns ``(status, detail)`` where status ∈ {healthy, warning, critical, unknown}.
    Rule: past the deadline + grace, a load must have arrived since the previous deadline;
    otherwise it is overdue (warning, escalating to critical after another grace period)."""
    f = resolve(policy)["freshness"]
    hhmm = _parse_hhmm(f.get("deadline_time"))
    if hhmm is None:
        return ("unknown", "Set a daily deadline time to enable deadline checks")
    dh, dm = hhmm
    cadence_h = _f(f.get("cadence_hours"), 24.0) or 24.0
    grace_s = _f(f.get("grace_minutes"), 30.0) * 60.0
    now = datetime.fromtimestamp(now_epoch, tz=UTC)
    today_due = now.replace(hour=dh, minute=dm, second=0, microsecond=0)
    d_due = today_due if now >= today_due else today_due - timedelta(days=1)
    grace_cutoff = d_due + timedelta(seconds=grace_s)
    prev = d_due - timedelta(hours=cadence_h)
    due_str = d_due.strftime("%H:%M")
    grace_m = int(grace_s // 60)
    if now < grace_cutoff:
        return ("healthy", f"On track — due by {due_str} UTC (+{grace_m}m grace)")
    present = last_load_epoch is not None and last_load_epoch > prev.timestamp()
    if present:
        ll = datetime.fromtimestamp(last_load_epoch, tz=UTC)
        return ("healthy", f"Met deadline — last load {ll.strftime('%b %d, %H:%M')} UTC")
    overdue_s = now.timestamp() - grace_cutoff.timestamp()
    status = CRITICAL if overdue_s > grace_s else WARNING
    mins = int(overdue_s // 60)
    late = f"{mins}m" if mins < 90 else f"{mins // 60}h{mins % 60:02d}m"
    return (status, f"Overdue — expected by {due_str} UTC, {late} late")


def freshness_status(
    lag_seconds: float | None, last_load_epoch: float | None, now_epoch: float, policy: dict | None
) -> tuple[str, str | None]:
    """Unified freshness verdict for the UI card — dispatches on the policy's mode."""
    f = resolve(policy)["freshness"]
    if str(f.get("mode")) == "deadline" and f.get("deadline_time"):
        return freshness_deadline_status(last_load_epoch, now_epoch, policy)
    sev = freshness_severity(lag_seconds, policy) if lag_seconds is not None else None
    return ({None: "healthy", WARNING: "warning", CRITICAL: "critical"}[sev], None)


# ----------------------------- infer freshness config from past loads -----------------------------
_CADENCES = (1, 2, 3, 4, 6, 8, 12, 24)


def _snap_cadence(hours: float) -> int:
    return min(_CADENCES, key=lambda c: abs(c - hours))


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _round5(x: float) -> int:
    return int(round(x / 5.0) * 5)


def infer_freshness(load_epochs: list[float]) -> dict | None:
    """Infer a freshness policy from observed load times (the `last_load` metric series).

    Distinct load timestamps reveal the cadence (median gap) and, for daily loads, the typical
    time-of-day a load completes — so we can suggest a deadline + grace. Sub-daily cadences get
    staleness thresholds instead. Returns None when there isn't enough history (< 3 loads)."""
    loads = sorted({round(float(e)) for e in load_epochs if e})
    if len(loads) < 3:
        return None
    gaps_h = [(loads[i] - loads[i - 1]) / 3600.0 for i in range(1, len(loads))]
    med_gap = statistics.median(gaps_h) or 24.0
    cadence = _snap_cadence(med_gap)
    sd = statistics.pstdev(gaps_h) if len(gaps_h) > 1 else 0.0
    confidence = round(max(0.0, min(1.0, 1.0 - (sd / med_gap if med_gap else 1.0))), 2)
    dts = [datetime.fromtimestamp(e, tz=UTC) for e in loads]

    if cadence >= 20:  # daily → deadline mode from the time-of-day distribution
        tod = sorted(float(dt.hour * 60 + dt.minute) for dt in dts)
        p50, p90 = _pct(tod, 50), _pct(tod, 90)
        spread = max(p90 - p50, 10.0)
        grace = int(min(180, max(15, _round5(spread + 10))))
        deadline_min = int(min(24 * 60 - 1, _round5(p90 + spread)))
        return {
            "mode": "deadline",
            "source": "watermark",
            "cadence_hours": 24,
            "deadline_time": f"{deadline_min // 60:02d}:{deadline_min % 60:02d}",
            "grace_minutes": grace,
            "typical_time": f"{int(p50) // 60:02d}:{int(p50) % 60:02d}",
            "confidence": confidence,
            "loads_observed": len(loads),
        }
    return {  # sub-daily → staleness thresholds derived from the cadence
        "mode": "staleness",
        "source": "watermark",
        "cadence_hours": cadence,
        "warn_after_hours": round(cadence * 1.25, 1),
        "severe_after_hours": round(cadence * 2.0, 1),
        "confidence": confidence,
        "loads_observed": len(loads),
    }


def quality_severity(score: float, policy: dict | None) -> str | None:
    """Severity for a table quality score, or ``None`` when the score is acceptable."""
    q = resolve(policy)["quality"]
    if score < _f(q.get("severe_below"), 60.0):
        return CRITICAL
    if score < _f(q.get("warn_below"), 80.0):
        return WARNING
    return None


def quality_floor(policy: dict | None) -> float:
    """The score at/above which quality is healthy (the warn threshold)."""
    return _f(resolve(policy)["quality"].get("warn_below"), 80.0)


def volume_severity(pct_abs: float | None, z_abs: float | None, policy: dict | None) -> str:
    """Severity for a volume anomaly.

    Uses configured % deviation thresholds when the monitor sets them; otherwise falls back
    to the historical z-score rule (critical at |z| >= 6, warning otherwise).
    """
    v = resolve(policy)["volume"]
    warn_pct, severe_pct = v.get("warn_pct"), v.get("severe_pct")
    if pct_abs is not None and (warn_pct is not None or severe_pct is not None):
        if severe_pct is not None and pct_abs >= _f(severe_pct, 1e9):
            return CRITICAL
        if warn_pct is not None and pct_abs >= _f(warn_pct, 1e9):
            return WARNING
        return WARNING
    return CRITICAL if (z_abs is not None and z_abs >= 6) else WARNING
