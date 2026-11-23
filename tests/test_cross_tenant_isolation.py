"""Cross-tenant isolation for the models that are NOT tenant-scoped themselves.

`RuleResult` and `CompareResult` carry no org_id/group_id — they're reached only through a
tenant-scoped parent (`DQRule`/`CompareJob`). That's safe *only* while every read first proves
the parent belongs to the caller. These tests pin that invariant: Group B, even as an admin,
must never reach Group A's rule history, compare results, or project test-case listing. A future
refactor that drops a parent-ownership check will turn one of these green→red immediately.

Real metadata store + ResultStore, no mocks.
"""

from __future__ import annotations

import pytest

from dvp.core.errors import NotFoundError
from dvp.observability.observability_service import RuleEvaluationService
from dvp.observability.service import MonitorService, RuleService
from dvp.services.connections import ConnectionService
from dvp.services.projects import ProjectService
from dvp.testing_engine.service import CompareService

TABLE = "RAW_SALES.PUBLIC.RAW_ORDERS"
COORDS = {
    "database": "RAW_SALES",
    "schema": "PUBLIC",
    "tables": {
        "RAW_ORDERS": {
            "columns": [{"name": "ORDER_ID", "data_type": "NUMBER", "nullable": False}],
            "rows": [{"ORDER_ID": 1}, {"ORDER_ID": 2}],
        }
    },
}


# ---------------------------------------------------------------- CompareResult (child of CompareJob)


def _run_compare(container, ctx):
    svc = CompareService(container.metadata, container.results)
    job = svc.create_job(
        ctx, kind="data", source={"table": "SRC"}, target={"table": "DST"}, options={}
    )
    svc.run_inmemory(
        ctx,
        job["id"],
        src_rows=[{"ID": 1, "AMT": "10.00"}, {"ID": 2, "AMT": "20.00"}],
        dst_rows=[{"ID": 1, "AMT": "10.00"}, {"ID": 2, "AMT": "99.00"}],
        key=["ID"],
        columns=["AMT"],
        column_types={"AMT": "NUMBER"},
    )
    return job["id"]


def test_compare_job_and_results_isolated(container, ctx, other_group_ctx):
    svc = CompareService(container.metadata, container.results)
    job_id = _run_compare(container, ctx)

    # Group A reads its own job + cell-level differences (the CompareResult child row).
    assert svc.get_job(ctx, job_id)["id"] == job_id
    assert len(svc.read_differences(ctx, job_id, bucket="value_difference")) == 1

    # Group B cannot fetch the job, its differences, or its CSV export — blocked at the parent gate.
    with pytest.raises(NotFoundError):
        svc.get_job(other_group_ctx, job_id)
    with pytest.raises(NotFoundError):
        svc.read_differences(other_group_ctx, job_id, bucket="value_difference")
    with pytest.raises(NotFoundError):
        svc.export_differences_csv(other_group_ctx, job_id)

    # B's own compare list is empty; A still sees exactly its one job.
    assert svc.list_jobs(other_group_ctx) == []
    assert [j["id"] for j in svc.list_jobs(ctx)] == [job_id]


# ---------------------------------------------------------------- RuleResult (child of DQRule)


def _rule_with_history(container, ctx):
    conn = ConnectionService(container.metadata, container.secrets).create(
        ctx, source_type="local", name="orders", coordinates=COORDS, auth_method="none"
    )
    mid = MonitorService(container.metadata).create(
        ctx,
        connection_id=conn["id"],
        name="m",
        scope={"database": "RAW_SALES", "schema": "PUBLIC", "tables": ["RAW_ORDERS"]},
    )["id"]
    rid = RuleService(container.metadata).create(
        ctx,
        mid,
        {"name": "neg", "type": "custom_sql", "table_ref": TABLE, "config": {"threshold": 0}},
    )["id"]
    ev = RuleEvaluationService(container.metadata)
    ev.record_result(
        ctx, mid, rid, {"table_ref": TABLE, "metric_value": 3, "ts": "2026-10-03T00:00:00Z"}
    )
    return mid, rid


def test_rule_history_isolated(container, ctx, other_group_ctx):
    ev = RuleEvaluationService(container.metadata)
    mid, rid = _rule_with_history(container, ctx)

    # Group A sees the recorded RuleResult history.
    assert len(ev.history(ctx, mid, rid)["points"]) == 1

    # Group B, knowing the ids, still cannot read the history — the _load_rule gate stops it
    # before the (un-scoped) RuleResult query runs.
    with pytest.raises(NotFoundError):
        ev.history(other_group_ctx, mid, rid)

    # Nor can B record a result against A's rule (poisoning the trail).
    with pytest.raises(NotFoundError):
        ev.record_result(other_group_ctx, mid, rid, {"table_ref": TABLE, "metric_value": 0})


# ---------------------------------------------------------------- Project test-case listing


def test_project_test_cases_isolated(container, admin_ctx, other_group_admin_ctx):
    """test_cases() queries CompareJob by project_id and relies on the project-ownership gate;
    Group B must not be able to enumerate Group A's project test cases."""
    projects = ProjectService(container.metadata)
    proj = projects.create(admin_ctx, name="A-proj")
    CompareService(container.metadata, container.results).create_job(
        admin_ctx,
        kind="data",
        source={"table": "S"},
        target={"table": "D"},
        options={},
        project_id=proj["id"],
    )

    assert len(projects.test_cases(admin_ctx, proj["id"])["compares"]) == 1
    with pytest.raises(NotFoundError):
        projects.test_cases(other_group_admin_ctx, proj["id"])
