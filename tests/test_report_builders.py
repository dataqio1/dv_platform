"""Pure report-builder aggregation + a service-level data-quality report (real DB)."""

from __future__ import annotations

from dvp.db.base import new_id, utcnow
from dvp.db.models import Connection, DQRule, Monitor, RuleResult
from dvp.reporting import builders
from dvp.reporting.service import ReportService


def test_build_data_validation_pass_fail_mix():
    jobs = [
        {"id": "1", "name": "ok", "kind": "data", "source": {"table": "A"}, "target": {"table": "A"},
         "status": "succeeded",
         "summary": {"matched_rows": 3, "value_difference_rows": 0, "source_only_rows": 0,
                     "dst_only_rows": 0, "ambiguous_rows": 0, "match_rate_pct": 100.0,
                     "src_row_count": 3, "dst_row_count": 3, "engine": "inmemory"}},
        {"id": "2", "name": "bad", "kind": "data", "source": {"table": "B"}, "target": {"table": "B"},
         "status": "succeeded",
         "summary": {"matched_rows": 1, "value_difference_rows": 1, "source_only_rows": 1,
                     "dst_only_rows": 0, "ambiguous_rows": 0, "match_rate_pct": 50.0,
                     "src_row_count": 3, "dst_row_count": 2, "engine": "hash"}},
        {"id": "3", "name": "cnt", "kind": "count", "source": {"table": "C"}, "target": {"table": "C"},
         "status": "succeeded", "summary": {"src_count": 10, "dst_count": 10, "diff": 0, "match": True}},
    ]
    summary, doc = builders.build_data_validation(jobs)
    assert summary["total_test_cases"] == 3
    assert summary["passed"] == 2 and summary["failed"] == 1
    assert summary["pass_rate_pct"] == 66.67
    assert summary["by_kind"]["data"] == {"total": 2, "passed": 1}
    assert summary["row_totals"]["value_difference_rows"] == 1
    assert summary["avg_match_rate_pct"] == 75.0
    assert len(doc["diagnosis"]) == 1 and doc["diagnosis"][0]["name"] == "bad"


def test_build_data_quality_rollup():
    rules = [
        {"id": "1", "name": "not_null", "table_ref": "DB.S.ORDERS", "dimension": "quality",
         "severity": "critical", "passed": True, "metric_value": 0, "threshold": 0},
        {"id": "2", "name": "freshness", "table_ref": "DB.S.ORDERS", "dimension": "freshness",
         "severity": "warning", "passed": False, "metric_value": 48, "threshold": 24,
         "observed": {"hours": 48}},
    ]
    summary, doc = builders.build_data_quality(rules)
    assert summary["total_rules"] == 2 and summary["passed"] == 1
    assert summary["pass_rate_pct"] == 50.0
    assert summary["by_dimension"]["freshness"] == {"total": 1, "passed": 0}
    assert doc["by_table"]["DB.S.ORDERS"]["pass_rate_pct"] == 50.0
    assert len(doc["failing_rules"]) == 1


def test_build_executive_health_score():
    _, v = builders.build_data_validation(
        [{"id": "1", "name": "x", "kind": "count", "source": {}, "target": {},
          "status": "ok", "summary": {"match": True}}]
    )
    _, q = builders.build_data_quality(
        [{"id": "r", "name": "n", "table_ref": "T", "dimension": "quality", "passed": False}]
    )
    summary, _doc = builders.build_executive(v, q, {"open": 2})
    assert summary["validation_pass_rate_pct"] == 100.0
    assert summary["quality_pass_rate_pct"] == 0.0
    assert summary["health_score_pct"] == 50.0
    assert summary["open_incidents"] == 2


def _seed_rule(container, ctx, *, passed: bool, dimension: str):
    with container.metadata.session(ctx) as s:
        conn = Connection(id=new_id(), org_id=ctx.org_id, group_id=ctx.group_id,
                          source_type="local", name="c", auth_method="none", coordinates={})
        s.add(conn)
        s.flush()
        mon = Monitor(id=new_id(), org_id=ctx.org_id, group_id=ctx.group_id, connection_id=conn.id,
                      name="m", scope={})
        s.add(mon)
        s.flush()
        rule = DQRule(id=new_id(), org_id=ctx.org_id, group_id=ctx.group_id, monitor_id=mon.id,
                      table_ref="DB.S.T", name="r", type="custom_sql",
                      config={"dimension": dimension}, severity="critical")
        s.add(rule)
        s.flush()
        s.add(RuleResult(id=new_id(), dq_rule_id=rule.id, table_ref="DB.S.T", passed=passed,
                         metric_value=0.0, threshold=0.0, created_at=utcnow()))


def test_data_quality_report_service(container, ctx):
    _seed_rule(container, ctx, passed=True, dimension="quality")
    _seed_rule(container, ctx, passed=False, dimension="freshness")
    svc = ReportService(container.metadata, container.results)
    rep = svc.create_report(ctx, name="DQ", report_type="data_quality")
    gen = svc.generate(ctx, rep["id"])
    assert gen["status"] == "ready"
    assert gen["summary"]["total_rules"] == 2
    assert gen["summary"]["passed"] == 1
    doc = svc.get_report(ctx, rep["id"])["document"]
    assert len(doc["failing_rules"]) == 1
    # CSV export lists both rules
    content, media = svc.export(ctx, rep["id"], "csv")
    assert media == "text/csv" and content.count("\n") >= 2
