"""Historical-baseline DQ rule evaluation — pure logic (no DB, no I/O).

Validates a new numeric observation against a baseline computed from the metric's own
history. Covers Phase-1 rule types: deviation, monotonic (trend), and growth. Pure functions
over an in-memory history list, so the PDF acceptance criteria map 1:1 onto unit tests and the
same code runs on any source (SQL / file / dashboard) — all produce the same Observation.

Design points from the requirements:
* Values use Decimal (NFR-5) so large financial totals don't false-trip on float error.
* Baseline excludes Excluded / Error / unaccepted-Fail observations (FR-B2) and anything before
  a declared reset date (FR-B6 / FR-R3).
* Cold start: < min_history valid points → Learning, unless a static fallback threshold is set
  (FR-B3 / FR-B4).
* Deviation supports separate upper/lower tolerances and one- or two-sided direction (FR-R5).
* Every result carries the baseline, deviation (abs + %), the limit applied, and a plain-language
  message (FR-E1 / FR-E2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# Result statuses (FR §Evaluation).
PASS, WARN, FAIL, LEARNING, ERROR = "pass", "warn", "fail", "learning", "error"

BASELINE_RULE_TYPES = frozenset({"deviation", "monotonic", "growth"})
_AGGREGATES = frozenset({"avg", "median", "last", "min", "max"})
_MONOTONIC_DIRS = frozenset(
    {"non_decreasing", "strictly_increasing", "non_increasing", "strictly_decreasing"}
)
DEFAULT_MIN_HISTORY = 5


def _d(x: object) -> Decimal | None:
    if x is None:
        return None
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return None


@dataclass
class Observation:
    """One captured value of a metric. `status`/`excluded`/`accepted` drive baseline validity."""

    value: object  # int | float | Decimal | str (coerced to Decimal)
    ts: datetime | None = None
    business_date: date | None = None
    status: str = PASS  # pass | warn | fail | learning | error
    excluded: bool = False
    accepted: bool = False  # a failed value the user accepted as the new normal


@dataclass
class BaselineResult:
    status: str
    new_value: Decimal | None
    baseline_value: Decimal | None = None
    deviation_abs: Decimal | None = None
    deviation_pct: float | None = None
    limit: str | None = None
    used: int = 0
    message: str = ""
    observed: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "new_value": None if self.new_value is None else float(self.new_value),
            "baseline_value": None if self.baseline_value is None else float(self.baseline_value),
            "deviation_abs": None if self.deviation_abs is None else float(self.deviation_abs),
            "deviation_pct": self.deviation_pct,
            "limit": self.limit,
            "used": self.used,
            "message": self.message,
            "observed": self.observed,
        }


# ----------------------------- history helpers -----------------------------
def _reset_cutoff(reset: object) -> datetime | date | None:
    if reset is None:
        return None
    if isinstance(reset, (datetime, date)):
        return reset
    try:
        return datetime.fromisoformat(str(reset))
    except ValueError:
        return None


def _after_reset(o: Observation, cutoff: datetime | date | None) -> bool:
    if cutoff is None:
        return True
    stamp = o.business_date or (o.ts.date() if isinstance(o.ts, datetime) else o.ts)
    cut = cutoff.date() if isinstance(cutoff, datetime) else cutoff
    if stamp is None:
        return True
    return stamp >= cut


def valid_history(history: list[Observation], reset: object = None) -> list[Decimal]:
    """Observations usable in a baseline: not Excluded, not Error, not an unaccepted Fail, with a
    parseable value, on/after any declared reset date (FR-B2 / FR-B6)."""
    cutoff = _reset_cutoff(reset)
    out: list[Decimal] = []
    for o in history:
        if o.excluded or o.status == ERROR:
            continue
        if o.status == FAIL and not o.accepted:
            continue
        if not _after_reset(o, cutoff):
            continue
        v = _d(o.value)
        if v is None:
            continue
        out.append(v)
    return out


def _window(values: list[Decimal], window: dict | None) -> list[Decimal]:
    """Slice the (chronological) history to the rule's window. Only observation-count windows are
    sliced here; day/calendar windows are applied upstream when real timestamps are present."""
    if not window:
        return values
    n = window.get("observations")
    if n:
        return values[-int(n):]
    return values


def _aggregate(values: list[Decimal], kind: str) -> Decimal | None:
    if not values:
        return None
    if kind == "last":
        return values[-1]
    if kind == "min":
        return min(values)
    if kind == "max":
        return max(values)
    if kind == "avg":
        return sum(values) / Decimal(len(values))
    if kind == "median":
        s = sorted(values)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / Decimal(2)
    return None


def _pct(dev: Decimal, base: Decimal) -> float | None:
    if base == 0:
        return None
    return round(float(dev / base * 100), 4)


def _window_label(window: dict | None, aggregate: str) -> str:
    if window and window.get("days"):
        return f"{window['days']}-day {aggregate}"
    if window and window.get("observations"):
        return f"{aggregate} of last {window['observations']}"
    return aggregate


def _severity_status(severity: str) -> str:
    return FAIL if str(severity).lower() in (FAIL, "critical", "error") else WARN


# ----------------------------- rule evaluators -----------------------------
def evaluate_deviation(new_value: object, history: list[Observation], config: dict) -> BaselineResult:
    nv = _d(new_value)
    if nv is None:
        return BaselineResult(ERROR, None, message="value could not be parsed")
    severity = config.get("severity", "warn")
    min_history = int(config.get("min_history", DEFAULT_MIN_HISTORY))
    window = config.get("window") or config.get("baseline", {}).get("window")
    aggregate = (config.get("aggregate") or config.get("baseline", {}).get("aggregate") or "avg").lower()
    if aggregate not in _AGGREGATES:
        aggregate = "avg"

    hist = _window(valid_history(history, config.get("reset")), window)
    if len(hist) < min_history:
        return _learning_or_fallback(nv, severity, min_history, len(hist), config)

    base = _aggregate(hist, aggregate)
    dev = nv - base
    dev_pct = _pct(abs(dev), base)

    # Separate upper/lower limits (FR-R5); fall back to the symmetric one if only it is set.
    up = _limit(config, base, "upper")
    lo = _limit(config, base, "lower")
    direction = str(config.get("direction", "both")).lower()

    high = (direction in ("both", "upper")) and up is not None and dev > up
    low = (direction in ("both", "lower")) and lo is not None and (-dev) > lo

    label = _window_label(window, aggregate)
    if high or low:
        side = "above" if high else "below"
        lim = up if high else lo
        lim_lbl = _limit_label(config, "upper" if high else "lower", lim)
        msg = (
            f"{_num(nv)} is {dev_pct:.1f}% {side} {label} {_num(base)} (limit {lim_lbl})"
            if dev_pct is not None
            else f"{_num(nv)} is {side} {label} {_num(base)} by {_num(abs(dev))} (limit {lim_lbl})"
        )
        return BaselineResult(
            _severity_status(severity), nv, base, abs(dev), dev_pct, _num(lim),
            len(hist), msg, {"aggregate": aggregate, "direction": direction},
        )
    return BaselineResult(
        PASS, nv, base, abs(dev), dev_pct, None, len(hist),
        f"{_num(nv)} within tolerance of {label} {_num(base)}",
        {"aggregate": aggregate, "direction": direction},
    )


def _limit_label(config: dict, side: str, absolute: Decimal | None) -> str:
    """Human label for a limit — the configured percentage ("5%") when pct-based, else the
    absolute value, so the message reads like the spec's example (FR-E2)."""
    pct = config.get(f"tolerance_pct_{side}")
    if pct is None:
        pct = config.get("tolerance_pct")
    if pct is not None:
        return f"{_fmt(float(pct))}%"
    return _num(absolute)


def _limit(config: dict, base: Decimal, side: str) -> Decimal | None:
    """Absolute tolerance for `side` (upper|lower), from % or absolute config, with symmetric
    fallback."""
    pct = config.get(f"tolerance_pct_{side}")
    if pct is None:
        pct = config.get("tolerance_pct")
    ab = config.get(f"tolerance_abs_{side}")
    if ab is None:
        ab = config.get("tolerance_abs")
    if pct is not None:
        return abs(base) * _d(pct) / Decimal(100)
    if ab is not None:
        return _d(ab)
    return None


def evaluate_monotonic(new_value: object, history: list[Observation], config: dict) -> BaselineResult:
    nv = _d(new_value)
    if nv is None:
        return BaselineResult(ERROR, None, message="value could not be parsed")
    severity = config.get("severity", "warn")
    direction = str(config.get("direction", "non_decreasing")).lower()
    if direction not in _MONOTONIC_DIRS:
        direction = "non_decreasing"
    tol = _d(config.get("tolerance", 0)) or Decimal(0)

    hist = valid_history(history, config.get("reset"))
    if not hist:
        # No previous valid value (new metric, or history cleared by a reset): nothing to
        # violate — this observation establishes the baseline (FR-R2 / FR-R3).
        return BaselineResult(
            PASS, nv, None, None, None, None, 0,
            "first observation — baseline established", {"direction": direction},
        )
    prev = hist[-1]
    delta = nv - prev

    ok = _monotonic_ok(delta, direction, tol)
    if ok:
        return BaselineResult(
            PASS, nv, prev, abs(delta), _pct(abs(delta), prev), None, len(hist),
            f"{_num(nv)} satisfies {direction.replace('_', ' ')} vs previous {_num(prev)}",
            {"direction": direction},
        )
    return BaselineResult(
        _severity_status(severity), nv, prev, abs(delta), _pct(abs(delta), prev), None, len(hist),
        f"{_num(nv)} breaks {direction.replace('_', ' ')} vs previous {_num(prev)}",
        {"direction": direction},
    )


def _monotonic_ok(delta: Decimal, direction: str, tol: Decimal) -> bool:
    if direction == "non_decreasing":
        return delta >= -tol
    if direction == "strictly_increasing":
        return delta > tol
    if direction == "non_increasing":
        return delta <= tol
    if direction == "strictly_decreasing":
        return delta < -tol
    return True


def evaluate_growth(new_value: object, history: list[Observation], config: dict) -> BaselineResult:
    nv = _d(new_value)
    if nv is None:
        return BaselineResult(ERROR, None, message="value could not be parsed")
    severity = config.get("severity", "warn")
    hist = valid_history(history, config.get("reset"))
    if not hist:
        return BaselineResult(
            PASS, nv, None, None, None, None, 0,
            "first observation — no previous value to compare growth against", {},
        )
    prev = hist[-1]
    delta = nv - prev
    unit = str(config.get("unit", "absolute")).lower()
    change = _pct(delta, prev) if unit in ("pct", "percent", "%") else float(delta)

    lo = config.get("min_change")
    hi = config.get("max_change")
    lo_v = None if lo is None else float(lo)
    hi_v = None if hi is None else float(hi)
    cval = change if change is not None else 0.0

    below = lo_v is not None and cval < lo_v
    above = hi_v is not None and cval > hi_v
    unit_lbl = "%" if unit in ("pct", "percent", "%") else ""
    if below or above:
        bound = lo_v if below else hi_v
        word = "below min" if below else "above max"
        return BaselineResult(
            _severity_status(severity), nv, prev, abs(delta), _pct(abs(delta), prev),
            f"{bound}{unit_lbl}", len(hist),
            f"change {_fmt(cval)}{unit_lbl} vs previous {_num(prev)} is {word} ({_fmt(bound)}{unit_lbl})",
            {"change": cval, "unit": unit},
        )
    return BaselineResult(
        PASS, nv, prev, abs(delta), _pct(abs(delta), prev), None, len(hist),
        f"change {_fmt(cval)}{unit_lbl} within range vs previous {_num(prev)}",
        {"change": cval, "unit": unit},
    )


# ----------------------------- cold start -----------------------------
def _learning_or_fallback(
    nv: Decimal, severity: str, min_history: int, have: int, config: dict
) -> BaselineResult:
    """Too little history to baseline. If a static fallback threshold is configured, evaluate
    against it so a brand-new metric is still protected on day one (FR-B4); else Learning."""
    fb = config.get("fallback_threshold")
    if fb is not None:
        limit = _d(fb)
        fdir = str(config.get("fallback_direction", "upper")).lower()  # upper: fail if nv > limit
        breached = (nv > limit) if fdir == "upper" else (nv < limit)
        if breached:
            return BaselineResult(
                _severity_status(severity), nv, limit, abs(nv - limit), _pct(abs(nv - limit), limit),
                _num(limit), have, f"{_num(nv)} breaches fallback threshold {_num(limit)} (learning)",
                {"fallback": True},
            )
        return BaselineResult(
            PASS, nv, limit, abs(nv - limit), None, _num(limit), have,
            f"{_num(nv)} within fallback threshold (learning: {have}/{min_history} history)",
            {"fallback": True},
        )
    return BaselineResult(
        LEARNING, nv, None, None, None, None, have,
        f"learning — {have}/{min_history} valid observations before this rule evaluates",
    )


# ----------------------------- dispatch + formatting -----------------------------
def evaluate(rule_type: str, new_value: object, history: list[Observation], config: dict) -> BaselineResult:
    if rule_type == "deviation":
        return evaluate_deviation(new_value, history, config)
    if rule_type == "monotonic":
        return evaluate_monotonic(new_value, history, config)
    if rule_type == "growth":
        return evaluate_growth(new_value, history, config)
    raise ValueError(f"unknown baseline rule type: {rule_type}")


def _num(v: Decimal | None) -> str:
    if v is None:
        return "—"
    f = float(v)
    return f"{f:,.2f}".rstrip("0").rstrip(".") if f != int(f) else f"{int(f):,}"


def _fmt(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v):,}"
