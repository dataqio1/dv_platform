"""Historical-baseline metrics API — definitions, rules, runs, history, lifecycle.

A metric captures a numeric value (Phase 1: SQL) every run, stores it as history, and
evaluates its baseline rules against prior valid history. Tenant-scoped, gated, audited.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query, Response, status

from dvp.api.deps import ContainerDep, ContextDep
from dvp.api.schemas import (
    AcknowledgeTableSuggestions,
    MetricCreate,
    MetricReset,
    MetricRuleCreate,
    MetricRunRequest,
    ObservationExclude,
)
from dvp.core.api_response import ApiResponse
from dvp.observability.metrics import MetricService

router = APIRouter(prefix="/metrics", tags=["metrics"])


def _svc(container) -> MetricService:  # noqa: ANN001
    return MetricService(container.metadata, container.secrets)


@router.post("", status_code=status.HTTP_201_CREATED)
def create(body: MetricCreate, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(
        _svc(container).create(
            ctx, name=body.name, source_type=body.source_type, source_config=body.source_config,
            connection_id=body.connection_id, description=body.description,
            monitor_id=body.monitor_id, project_id=body.project_id,
            fallback_threshold=body.fallback_threshold, retention_days=body.retention_days,
            timezone=body.timezone, schedule_cron=body.schedule_cron,
        )
    )


@router.get("")
def list_metrics(
    ctx: ContextDep, container: ContainerDep,
    monitor_id: str | None = Query(None), project_id: str | None = Query(None),
) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(_svc(container).list(ctx, monitor_id=monitor_id, project_id=project_id))


@router.post("/from-table-suggestions", status_code=status.HTTP_201_CREATED)
def from_table_suggestions(
    body: AcknowledgeTableSuggestions, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Acknowledge load-pattern rule suggestions for a table → create a row-count metric + the
    chosen baseline rules (with any tweaked parameters)."""
    return ApiResponse.ok(
        _svc(container).create_from_table_suggestions(
            ctx, monitor_id=body.monitor_id, table_ref=body.table_ref,
            connection_id=body.connection_id, load_pattern=body.load_pattern,
            selections=[s.model_dump() for s in body.selections],
        )
    )


# --- literal sub-paths before /{metric_id} ---
@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(rule_id: str, ctx: ContextDep, container: ContainerDep) -> Response:
    _svc(container).delete_rule(ctx, rule_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/observations/{obs_id}:exclude")
def exclude_observation(
    obs_id: str, body: ObservationExclude, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(_svc(container).exclude(ctx, obs_id, body.reason))


@router.post("/observations/{obs_id}:accept")
def accept_observation(obs_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(_svc(container).accept(ctx, obs_id))


# --- metric by id ---
@router.get("/{metric_id}")
def get_metric(metric_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(_svc(container).get(ctx, metric_id))


@router.delete("/{metric_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_metric(metric_id: str, ctx: ContextDep, container: ContainerDep) -> Response:
    _svc(container).delete(ctx, metric_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{metric_id}/rules", status_code=status.HTTP_201_CREATED)
def add_rule(
    metric_id: str, body: MetricRuleCreate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(
        _svc(container).add_rule(ctx, metric_id, name=body.name, type=body.type,
                                 config=body.config, severity=body.severity)
    )


@router.get("/{metric_id}/observations")
def observations(
    metric_id: str, ctx: ContextDep, container: ContainerDep,
    dimension_key: str | None = Query(None), limit: int = Query(200, ge=1, le=1000),
) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(
        _svc(container).observations(ctx, metric_id, dimension_key=dimension_key, limit=limit)
    )


@router.post("/{metric_id}:run")
def run_metric(
    metric_id: str, ctx: ContextDep, container: ContainerDep, body: MetricRunRequest | None = None
) -> ApiResponse[dict]:
    body = body or MetricRunRequest()
    bd = date.fromisoformat(body.business_date) if body.business_date else None
    return ApiResponse.ok(
        _svc(container).run(ctx, metric_id, run_id=body.run_id, run_date=body.run_date, business_date=bd)
    )


@router.post("/{metric_id}:reset")
def reset_metric(
    metric_id: str, body: MetricReset, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(_svc(container).reset(ctx, metric_id, reset_at=body.reset_at, reason=body.reason))
