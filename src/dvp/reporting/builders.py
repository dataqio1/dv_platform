"""Pure report aggregation — turn raw executions/results into a report document + headline.

Mirrors dms-reporting's ReportsService / DataCompareReportService / DQ roll-ups, but kept
as pure functions over plain dicts so they're trivially testable and engine-agnostic. Each
builder returns ``(summary, document)``:
  * summary  — small headline dict stored in the metadata DB (list views, trends)
  * document — the full report (sections, per-test-case rows, diagnosis) stored in the
               ResultStore (large, D9-resident)
"""

from __future__ import annotations

from typing import Any


def _pct(num: float, den: float) -> float:
    return round(100.0 * num / den, 2) if den else 100.0


def _group(items: list[dict], key: str, passed) -> dict[str, dict]:  # noqa: ANN001
    """Group items by a field (source_system / layer) into {value: {total, passed, rate}}."""
    out: dict[str, dict] = {}
    for it in items:
        k = it.get(key) or "untagged"
        d = out.setdefault(k, {"total": 0, "passed": 0})
        d["total"] += 1
        d["passed"] += 1 if passed(it) else 0
    for d in out.values():
        d["pass_rate_pct"] = _pct(d["passed"], d["total"])
    return out


def job_passed(kind: str, summary: dict | None) -> bool:
    """Did a single test case pass? count/schema expose a `match` bool; data passes only
    when every "bad" bucket is empty."""
    if not summary:
        return False
    if kind in ("count", "schema"):
        return bool(summary.get("match"))
    bad = (
        summary.get("value_difference_rows", summary.get("mismatched_rows", 0))
        + summary.get("source_only_rows", 0)
        + summary.get("dst_only_rows", 0)
        + summary.get("ambiguous_rows", 0)
    )
    return bad == 0


def _diagnosis_reason(kind: str, summary: dict | None) -> str:
    if not summary:
        return "no result (not run or failed)"
    if kind == "count":
        return f"row counts differ by {summary.get('diff')} ({summary.get('diff_pct')}%)"
    if kind == "schema":
        return (
            f"{summary.get('type_mismatches', 0)} type mismatch(es), "
            f"{summary.get('src_only', 0)} source-only / {summary.get('dst_only', 0)} target-only column(s)"
        )
    return (
        f"{summary.get('value_difference_rows', summary.get('mismatched_rows', 0))} value diffs, "
        f"{summary.get('source_only_rows', 0)} source-only, "
        f"{summary.get('dst_only_rows', 0)} target-only, "
        f"{summary.get('ambiguous_rows', 0)} ambiguous"
    )


def build_data_validation(jobs: list[dict]) -> tuple[dict, dict]:
    """`jobs`: [{id, name, kind, source, target, status, summary, warnings}]."""
    total = len(jobs)
    passed = sum(1 for j in jobs if job_passed(j["kind"], j.get("summary")))
    by_kind: dict[str, dict] = {}
    agg = {
        "matched_rows": 0,
        "value_difference_rows": 0,
        "source_only_rows": 0,
        "dst_only_rows": 0,
        "ambiguous_rows": 0,
        "src_rows": 0,
        "dst_rows": 0,
    }
    match_rates: list[float] = []
    cases: list[dict] = []
    diagnosis: list[dict] = []

    for j in jobs:
        kind, s = j["kind"], j.get("summary") or {}
        ok = job_passed(kind, s)
        bk = by_kind.setdefault(kind, {"total": 0, "passed": 0})
        bk["total"] += 1
        bk["passed"] += 1 if ok else 0
        if kind == "data":
            agg["matched_rows"] += s.get("matched_rows", 0)
            agg["value_difference_rows"] += s.get(
                "value_difference_rows", s.get("mismatched_rows", 0)
            )
            agg["source_only_rows"] += s.get("source_only_rows", 0)
            agg["dst_only_rows"] += s.get("dst_only_rows", 0)
            agg["ambiguous_rows"] += s.get("ambiguous_rows", 0)
            agg["src_rows"] += s.get("src_row_count", 0)
            agg["dst_rows"] += s.get("dst_row_count", 0)
            if "match_rate_pct" in s:
                match_rates.append(s["match_rate_pct"])
        cases.append(
            {
                "id": j.get("id"),
                "name": j.get("name"),
                "kind": kind,
                "source": _side_label(j.get("source")),
                "target": _side_label(j.get("target")),
                "status": "pass" if ok else "fail",
                "engine": s.get("engine"),
                "summary": s,
                "warnings": j.get("warnings") or [],
                "src_count": s.get("src_count", s.get("src_row_count")),
                "dst_count": s.get("dst_count", s.get("dst_row_count")),
                "exception_keys": j.get("exception_keys") or [],
            }
        )
        if not ok:
            diagnosis.append(
                {
                    "id": j.get("id"),
                    "name": j.get("name"),
                    "kind": kind,
                    "reason": _diagnosis_reason(kind, s),
                }
            )

    _p = lambda j: job_passed(j["kind"], j.get("summary"))  # noqa: E731
    summary = {
        "report_type": "data_validation",
        "total_test_cases": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate_pct": _pct(passed, total),
        "by_kind": by_kind,
        "by_source": _group(jobs, "source_system", _p),
        "by_layer": _group(jobs, "layer", _p),
        "avg_match_rate_pct": round(sum(match_rates) / len(match_rates), 2)
        if match_rates
        else None,
        "row_totals": agg,
    }
    document = {**summary, "test_cases": cases, "diagnosis": diagnosis}
    if jobs and jobs[0].get("sign_off"):
        document["sign_off"] = {
            "ran_by": jobs[0].get("ran_by"),
            "ran_at": jobs[0].get("ran_at"),
            "batch_id": jobs[0].get("batch_id"),
            "tables": [
                {
                    "id": c["id"],
                    "table": (c.get("target") or c.get("name") or "").split(".")[-1],
                    "status": c["status"],
                    "src_count": c.get("src_count"),
                    "dst_count": c.get("dst_count"),
                    "exception_keys": c.get("exception_keys") or [],
                }
                for c in cases
            ],
        }
    return summary, document


def build_data_quality(rules: list[dict]) -> tuple[dict, dict]:
    """`rules`: [{id, name, table_ref, dimension, severity, monitor, passed, metric_value,
    threshold, observed}] — latest result per rule."""
    total = len(rules)
    passed = sum(1 for r in rules if r.get("passed"))
    by_dimension: dict[str, dict] = {}
    by_table: dict[str, dict] = {}
    failing: list[dict] = []

    for r in rules:
        dim = r.get("dimension") or "quality"
        d = by_dimension.setdefault(dim, {"total": 0, "passed": 0})
        d["total"] += 1
        d["passed"] += 1 if r.get("passed") else 0
        tref = r.get("table_ref") or "—"
        t = by_table.setdefault(tref, {"total": 0, "passed": 0})
        t["total"] += 1
        t["passed"] += 1 if r.get("passed") else 0
        if not r.get("passed"):
            failing.append(
                {
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "table_ref": r.get("table_ref"),
                    "dimension": dim,
                    "severity": r.get("severity"),
                    "metric_value": r.get("metric_value"),
                    "threshold": r.get("threshold"),
                    "observed": r.get("observed") or {},
                }
            )

    for t in by_table.values():
        t["pass_rate_pct"] = _pct(t["passed"], t["total"])

    summary = {
        "report_type": "data_quality",
        "total_rules": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate_pct": _pct(passed, total),
        "by_dimension": by_dimension,
        "by_source": _group(rules, "source_system", lambda r: r.get("passed")),
        "by_layer": _group(rules, "layer", lambda r: r.get("passed")),
        "tables_covered": len(by_table),
    }
    document = {**summary, "by_table": by_table, "failing_rules": failing, "rules": rules}
    return summary, document


def build_profile(tables: list[dict]) -> tuple[dict, dict]:
    """`tables`: [{table, row_count, columns: [{name, data_type, null_count, null_pct,
    distinct, min, max}]}] — one entry per profiled table."""
    cols = sum(len(t.get("columns", [])) for t in tables)
    rows = sum(t.get("row_count", 0) or 0 for t in tables)
    summary = {
        "report_type": "profile",
        "tables_profiled": len(tables),
        "columns_profiled": cols,
        "total_rows": rows,
    }
    return summary, {**summary, "tables": tables}


def build_executive(
    validation: dict | None, quality: dict | None, incidents: dict | None
) -> tuple[dict, dict]:
    """Roll up validation + quality headlines + open incidents into a health score."""
    v_rate = validation.get("pass_rate_pct") if validation else None
    q_rate = quality.get("pass_rate_pct") if quality else None
    scores = [r for r in (v_rate, q_rate) if r is not None]
    health = round(sum(scores) / len(scores), 2) if scores else None

    summary = {
        "report_type": "executive",
        "health_score_pct": health,
        "validation_pass_rate_pct": v_rate,
        "quality_pass_rate_pct": q_rate,
        "open_incidents": (incidents or {}).get("open", 0),
        "test_cases": (validation or {}).get("total_test_cases", 0),
        "rules": (quality or {}).get("total_rules", 0),
    }
    document = {
        **summary,
        "validation": validation,
        "quality": quality,
        "incidents": incidents or {},
    }
    return summary, document


def _side_label(spec: Any) -> str | None:
    if not isinstance(spec, dict):
        return None
    if spec.get("sql"):
        return "(sql)"
    parts = [spec.get("schema"), spec.get("table") or spec.get("view")]
    return ".".join(p for p in parts if p) or None
