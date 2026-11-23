"""Data-observability API: watermark detection/validation and per-table four-pillar
history with column-level drill-down.

Paths are declared in full (no router prefix) because they span both /connections and
/monitors; the router is mounted under /api/v1 in the app factory.
"""

from __future__ import annotations

from fastapi import APIRouter

from dvp.api.deps import ContainerDep, ContextDep
from dvp.api.schemas import (
    DryRunRules,
    IncidentComment,
    IncidentResolve,
    IncidentSilence,
    MonitorWatermark,
    PointExclude,
    RuleResultRecord,
    SnapshotObservation,
    SuggestFromSample,
    SuggestPack,
    WatermarkDetectRequest,
    WatermarkValidateRequest,
)
from dvp.core.api_response import ApiResponse, Warning_
from dvp.observability.incidents import IncidentService
from dvp.observability.observability_service import (
    ProfilerService,
    RuleEvaluationService,
    TableObservabilityService,
)
from dvp.observability.runner import MonitorRunner
from dvp.observability.service import MonitorService, RuleService
from dvp.services.watermark import WatermarkService

router = APIRouter(tags=["observability"])


def _lift_warnings(result: dict) -> ApiResponse[dict]:
    warnings = [Warning_(**w) for w in result.pop("warnings", [])]
    if warnings:
        return ApiResponse.with_warnings(result, warnings)
    return ApiResponse.ok(result)


# ----------------------------- watermark (Feature 1) -----------------------------
@router.post("/connections/{connection_id}/watermark:detect")
def detect_watermark(
    connection_id: str,
    body: WatermarkDetectRequest,
    ctx: ContextDep,
    container: ContainerDep,
) -> ApiResponse[dict]:
    """Auto-detect the best watermark column across the selected tables."""
    svc = WatermarkService(container.metadata, container.secrets)
    return _lift_warnings(svc.detect(ctx, connection_id, body.scope))


@router.post("/connections/{connection_id}/watermark:validate")
def validate_watermark(
    connection_id: str,
    body: WatermarkValidateRequest,
    ctx: ContextDep,
    container: ContainerDep,
) -> ApiResponse[dict]:
    """Validate that a watermark column exists (and is usable) in every table."""
    svc = WatermarkService(container.metadata, container.secrets)
    return _lift_warnings(
        svc.validate(ctx, connection_id, body.scope, body.column, body.expected_type)
    )


@router.post("/monitors/{monitor_id}:watermark")
def set_monitor_watermark(
    monitor_id: str,
    body: MonitorWatermark,
    ctx: ContextDep,
    container: ContainerDep,
) -> ApiResponse[dict]:
    svc = MonitorService(container.metadata)
    return ApiResponse.ok(
        svc.set_watermark(ctx, monitor_id, body.watermark_column, body.watermark_type)
    )


# ----------------------------- four pillars (Feature 2) -----------------------------
@router.get("/monitors/{monitor_id}/tables")
def list_tables(
    monitor_id: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(
        TableObservabilityService(container.metadata).list_tables(ctx, monitor_id)
    )


@router.post("/monitors/{monitor_id}/tables/{table_ref}/snapshot")
def record_snapshot(
    monitor_id: str,
    table_ref: str,
    body: SnapshotObservation,
    ctx: ContextDep,
    container: ContainerDep,
) -> ApiResponse[dict]:
    """Record a profiling snapshot (the write path that feeds pillar history)."""
    svc = ProfilerService(container.metadata, container.secrets)
    return ApiResponse.ok(
        svc.record_snapshot(ctx, monitor_id, table_ref, body.model_dump(exclude_none=True))
    )


@router.post("/monitors/{monitor_id}/tables/{table_ref}:profile")
def profile_table(
    monitor_id: str, table_ref: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Full column profile of one table. This scans the table. A scheduled check does not."""
    svc = ProfilerService(container.metadata, container.secrets)
    return ApiResponse.ok(svc.profile(ctx, monitor_id, table_ref))


@router.get("/monitors/{monitor_id}/tables/{table_ref}/freshness:suggest")
def suggest_freshness(
    monitor_id: str, table_ref: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Infer freshness config (deadline time / cadence / grace) from this table's load history."""
    return ApiResponse.ok(
        TableObservabilityService(container.metadata).suggest_freshness(ctx, monitor_id, table_ref)
    )


@router.get("/monitors/{monitor_id}/tables/{table_ref}/pillars")
def get_table_pillars(
    monitor_id: str, table_ref: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Current four-pillar status cards for a table (the table observability page)."""
    return ApiResponse.ok(
        TableObservabilityService(container.metadata).get_table_pillars(ctx, monitor_id, table_ref)
    )


@router.get("/monitors/{monitor_id}/tables/{table_ref}/pillars/{pillar}/history")
def get_pillar_history(
    monitor_id: str,
    table_ref: str,
    pillar: str,
    ctx: ContextDep,
    container: ContainerDep,
    limit: int = 1000,
    since_days: int | None = None,
) -> ApiResponse[dict]:
    """Pillar time-series with a rolling baseline band + anomaly flags. `since_days` sets
    the window (e.g. 7, 30, 365); omit for all history."""
    return ApiResponse.ok(
        TableObservabilityService(container.metadata).get_pillar_history(
            ctx, monitor_id, table_ref, pillar, limit, since_days
        )
    )


# ----------------------------- incidents (anomaly + rules, deduped) -----------------------------
@router.get("/incidents")
def list_incidents(
    ctx: ContextDep, container: ContainerDep, status: str | None = None, active_only: bool = False
) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(
        IncidentService(container.metadata).list(ctx, status=status, active_only=active_only)
    )


@router.get("/monitors/{monitor_id}/incidents")
def list_monitor_incidents(
    monitor_id: str, ctx: ContextDep, container: ContainerDep, active_only: bool = False
) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(
        IncidentService(container.metadata).list(
            ctx, monitor_id=monitor_id, active_only=active_only
        )
    )


@router.get("/monitors/{monitor_id}/tables/{table_ref}/incidents")
def list_table_incidents(
    monitor_id: str, table_ref: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(
        IncidentService(container.metadata).list(ctx, monitor_id=monitor_id, table_ref=table_ref)
    )


@router.post("/incidents/{incident_id}:ack")
def ack_incident(incident_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(
        IncidentService(container.metadata).set_status(ctx, incident_id, "acknowledged")
    )


@router.post("/incidents/{incident_id}:resolve")
def resolve_incident(
    incident_id: str, ctx: ContextDep, container: ContainerDep, body: IncidentResolve | None = None
) -> ApiResponse[dict]:
    """Resolve an alert, optionally with a disposition (ok / false_positive / confirmed) and a
    note. With no body it just marks the incident resolved (back-compat)."""
    svc = IncidentService(container.metadata)
    if body is None:
        return ApiResponse.ok(svc.set_status(ctx, incident_id, "resolved"))
    return ApiResponse.ok(svc.resolve(ctx, incident_id, resolution=body.resolution, note=body.note))


@router.post("/incidents/{incident_id}:reopen")
def reopen_incident(
    incident_id: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(IncidentService(container.metadata).reopen(ctx, incident_id))


@router.post("/incidents/{incident_id}:silence")
def silence_incident(
    incident_id: str, body: IncidentSilence, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Suppress alerting for this incident (or its whole monitor) until a point in time."""
    return ApiResponse.ok(
        IncidentService(container.metadata).silence(
            ctx,
            incident_id,
            until=body.until,
            duration_hours=body.duration_hours,
            scope=body.scope,
            note=body.note,
            ticket_url=body.ticket_url,
        )
    )


@router.post("/incidents/{incident_id}:unsilence")
def unsilence_incident(
    incident_id: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(IncidentService(container.metadata).unsilence(ctx, incident_id))


@router.get("/incidents/{incident_id}/comments")
def list_incident_comments(
    incident_id: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(IncidentService(container.metadata).comments(ctx, incident_id))


@router.post("/incidents/{incident_id}/comments", status_code=201)
def add_incident_comment(
    incident_id: str, body: IncidentComment, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(IncidentService(container.metadata).add_comment(ctx, incident_id, body.body))


@router.post("/observability/points/{point_id}:exclude")
def exclude_point(
    point_id: str, body: PointExclude, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Exclude (or restore) a single observation point from charts + baselines. The user's
    choice when a value is wrong — e.g. data loaded twice or a load was missed."""
    return ApiResponse.ok(
        TableObservabilityService(container.metadata).set_point_excluded(ctx, point_id, body.excluded)
    )


# ----------------------------- pushdown evaluator + run (Phase C2) -----------------------------
@router.post("/monitors/{monitor_id}:run")
def run_monitor(monitor_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Check every table in scope (watermark and a cheap count) and evaluate active rules."""
    svc = MonitorRunner(container.metadata, container.secrets)
    return ApiResponse.ok(svc.run_monitor(ctx, monitor_id))


@router.post("/monitors/{monitor_id}/rules:suggest-from-sample")
def suggest_rules_from_sample(
    monitor_id: str, body: SuggestFromSample, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Profile a sample (latest watermark batch, else ~N rows) and return SUGGESTED DQ rules at the
    chosen detail level (1/2/3). Nothing is saved — the user reviews/edits, then creates the ones
    they want. Works for table / SQL / file (S3) monitors."""
    svc = MonitorRunner(container.metadata, container.secrets)
    return ApiResponse.ok(
        svc.suggest_from_sample(ctx, monitor_id, level=body.level, sample_size=body.sample_size)
    )


@router.post("/monitors/{monitor_id}/rules:suggest-pack")
def suggest_pack(
    monitor_id: str, body: SuggestPack, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Auto-generate a suggested DQ rule pack for selected tables from a SAMPLE of the last batch
    (never a full scan). Each rule is created as state='suggested', origin='system' so the user
    accepts or removes it. Idempotent: an existing rule for the same (table, type, name) is skipped.
    Set persist=false to preview (specs + sample dry-run) without creating anything."""
    runner = MonitorRunner(container.metadata, container.secrets)
    pack = runner.generate_pack(
        ctx, monitor_id, tables=body.tables, sample_size=body.sample_size, level=body.level
    )
    if not body.persist:
        return ApiResponse.ok({**pack, "persisted": False})

    rules = RuleService(container.metadata)
    existing = {(r.get("table_ref"), r["type"], r["name"]) for r in rules.list(ctx, monitor_id)}
    created, skipped = [], []
    for tbl in pack["tables"]:
        for sp in tbl.get("rules", []):
            key = (sp.get("table_ref"), sp["type"], sp["name"])
            if key in existing:
                skipped.append(sp["name"])
                continue
            existing.add(key)
            spec = {**sp, "state": "suggested", "origin": "system"}
            spec.pop("dry_run", None)
            created.append(rules.create(ctx, monitor_id, spec))
    return ApiResponse.ok(
        {**pack, "persisted": True, "created": created, "skipped": skipped},
        meta={"created": len(created), "skipped": len(skipped)},
    )


@router.post("/monitors/{monitor_id}/rules:dry-run")
def dry_run_rules(
    monitor_id: str, body: DryRunRules, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Test rule specs against the sample (no persistence) — pass/fail + a few failing examples."""
    svc = MonitorRunner(container.metadata, container.secrets)
    return ApiResponse.ok(
        svc.dry_run_rules(ctx, monitor_id, body.rules, sample_size=body.sample_size)
    )


@router.post("/monitors/{monitor_id}/rules/{rule_id}:evaluate")
def evaluate_rule(
    monitor_id: str,
    rule_id: str,
    ctx: ContextDep,
    container: ContainerDep,
    table_ref: str | None = None,
) -> ApiResponse[dict]:
    """Evaluate one rule now via pushdown SQL and record the result."""
    svc = MonitorRunner(container.metadata, container.secrets)
    return ApiResponse.ok(svc.evaluate_rule(ctx, monitor_id, rule_id, table_ref))


# ----------------------------- custom DQ rules + metrics (Phase C1) -----------------------------
@router.post("/monitors/{monitor_id}/rules/{rule_id}:record")
def record_rule_result(
    monitor_id: str,
    rule_id: str,
    body: RuleResultRecord,
    ctx: ContextDep,
    container: ContainerDep,
) -> ApiResponse[dict]:
    """Record one rule evaluation → rule_result + `rule.<id>` metric."""
    svc = RuleEvaluationService(container.metadata)
    return ApiResponse.ok(
        svc.record_result(ctx, monitor_id, rule_id, body.model_dump(exclude_none=True))
    )


@router.get("/monitors/{monitor_id}/rules/{rule_id}/history")
def rule_history(
    monitor_id: str,
    rule_id: str,
    ctx: ContextDep,
    container: ContainerDep,
    limit: int = 100,
) -> ApiResponse[dict]:
    """A rule's metric + pass/fail time series."""
    return ApiResponse.ok(
        RuleEvaluationService(container.metadata).history(ctx, monitor_id, rule_id, limit)
    )


@router.get("/monitors/{monitor_id}/tables/{table_ref}/rules")
def rules_for_table(
    monitor_id: str, table_ref: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[list[dict]]:
    """Rules bound to a table (or monitor-wide) with their latest result."""
    return ApiResponse.ok(
        RuleEvaluationService(container.metadata).rules_for_table(ctx, monitor_id, table_ref)
    )


@router.get("/monitors/{monitor_id}/tables/{table_ref}/columns")
def list_columns(
    monitor_id: str, table_ref: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(
        TableObservabilityService(container.metadata).get_table_columns(ctx, monitor_id, table_ref)
    )


@router.get("/monitors/{monitor_id}/tables/{table_ref}/columns/{column}/history")
def get_column_history(
    monitor_id: str,
    table_ref: str,
    column: str,
    ctx: ContextDep,
    container: ContainerDep,
    limit: int = 100,
) -> ApiResponse[dict]:
    """Column-level drill-down: metric history for one column."""
    return ApiResponse.ok(
        TableObservabilityService(container.metadata).get_column_history(
            ctx, monitor_id, table_ref, column, limit
        )
    )
