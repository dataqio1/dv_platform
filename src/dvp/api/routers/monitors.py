from __future__ import annotations

from fastapi import APIRouter, status

from dvp.api.deps import ContainerDep, ContextDep
from dvp.api.schemas import (
    AutogenRequest,
    MonitorCreate,
    MonitorLoadType,
    MonitorStatus,
    MonitorUpdate,
    RuleCreate,
    RuleUpdate,
    TagAssignment,
)
from dvp.core.api_response import ApiResponse, Warning_
from dvp.observability.autogen import ColumnProfile, TableProfile, generate_rules
from dvp.observability.service import MonitorService, RuleService, resolve_schedule

router = APIRouter(prefix="/monitors", tags=["monitors"])


@router.post("", status_code=status.HTTP_201_CREATED)
def create(body: MonitorCreate, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    svc = MonitorService(container.metadata)
    return ApiResponse.ok(
        svc.create(
            ctx,
            connection_id=body.connection_id,
            name=body.name,
            scope=body.scope,
            description=body.description,
            tags=body.tags,
            schedule_cron=resolve_schedule(body.schedule, body.schedule_cron),
            watermark_column=body.watermark_column,
            watermark_type=body.watermark_type,
            load_type=body.load_type,
            lookback_days=body.lookback_days,
            alert_policy=body.alert_policy.model_dump(by_alias=True) if body.alert_policy else None,
            project_id=body.project_id,
            source_system=body.source_system,
            layer=body.layer,
            metadata_columns=body.metadata_columns,
            metadata_convention_ids=body.metadata_convention_ids,
        )
    )


@router.post("/{monitor_id}:load-type")
def set_load_type(
    monitor_id: str, body: MonitorLoadType, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Set how the table is loaded: incremental | full_reload (truncate & load) | append."""
    return ApiResponse.ok(
        MonitorService(container.metadata).set_load_type(ctx, monitor_id, body.load_type)
    )


@router.post("/{monitor_id}:detect-load-type")
def detect_load_type(
    monitor_id: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Classify how each table in the monitor is loaded (scd1 | scd2 | append | full_reload |
    other) and suggest column mappings by role (watermark/LOADED_AT, BATCH_ID, BUSINESS_KEY,
    VALID_FROM, IS_CURRENT, INGESTED_AT, EVENT_ID). Detect-and-accept counterpart to
    :load-type, which only SETS the value. Per-table schema-fetch failures surface as warnings."""
    from dvp.services.load_type_detector import LoadTypeDetectorService

    svc = LoadTypeDetectorService(container.metadata, container.secrets)
    result = svc.detect(ctx, monitor_id)
    warnings = [Warning_(**w) for w in result.pop("warnings", [])]
    return (
        ApiResponse.with_warnings(result, warnings) if warnings else ApiResponse.ok(result)
    )


@router.post("/{monitor_id}:detect-load-type-async")
def detect_load_type_async(
    monitor_id: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Start a BACKGROUND load-type detection over every table in the monitor's scope and return a
    run id immediately. Results stream in per table — poll GET /load-type-detections/{run_id} for
    progress + the per-table results. Use this for large schemas (100+ tables) so the UI stays
    responsive and can show a progressive results table."""
    from dvp.services.load_type_detector import LoadTypeDetectorService

    svc = LoadTypeDetectorService(container.metadata, container.secrets)
    run = svc.start_for_monitor(ctx, monitor_id)
    # Process on the worker pool when available, else inline (small/dev deployments).
    if hasattr(container, "jobs") and hasattr(container.jobs, "submit"):
        container.jobs.submit(svc.run_detection, ctx, run["id"])
    else:
        svc.run_detection(ctx, run["id"])
        run = svc.get(ctx, run["id"])
    return ApiResponse.ok(run)


@router.get("/load-type-detections/{run_id}")
def get_detection_run(
    run_id: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Poll a background detection run: status, progress {pct, processed, total}, and the per-table
    results accumulated so far."""
    from dvp.services.load_type_detector import LoadTypeDetectorService

    return ApiResponse.ok(LoadTypeDetectorService(container.metadata, container.secrets).get(ctx, run_id))


@router.patch("/{monitor_id}/assignment")
def assign(
    monitor_id: str, body: TagAssignment, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Set this DQ test case's project / source system / layer / tags."""
    return ApiResponse.ok(
        MonitorService(container.metadata).assign(
            ctx,
            monitor_id,
            project_id=body.project_id,
            source_system=body.source_system,
            layer=body.layer,
            tags=body.tags,
        )
    )


@router.get("")
def list_monitors(ctx: ContextDep, container: ContainerDep) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(MonitorService(container.metadata).list(ctx))


@router.get(":summary")
def monitors_summary(ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Health KPIs + status-chip counts for the monitors dashboard (screen 1)."""
    return ApiResponse.ok(MonitorService(container.metadata).summary(ctx))


@router.get("/{monitor_id}")
def get(monitor_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(MonitorService(container.metadata).get(ctx, monitor_id))


@router.patch("/{monitor_id}")
def update(
    monitor_id: str, body: MonitorUpdate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    fields = body.model_dump(exclude_unset=True)
    return ApiResponse.ok(MonitorService(container.metadata).update(ctx, monitor_id, fields))


@router.delete("/{monitor_id}")
def remove(monitor_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(MonitorService(container.metadata).remove(ctx, monitor_id))


@router.post("/{monitor_id}:status")
def set_status(
    monitor_id: str, body: MonitorStatus, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(
        MonitorService(container.metadata).set_status(ctx, monitor_id, body.status)
    )


# ----------------------------- rules -----------------------------
@router.get("/{monitor_id}/rules")
def list_rules(
    monitor_id: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(RuleService(container.metadata).list(ctx, monitor_id))


@router.post("/{monitor_id}/rules", status_code=status.HTTP_201_CREATED)
def create_rule(
    monitor_id: str, body: RuleCreate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(
        RuleService(container.metadata).create(ctx, monitor_id, body.model_dump())
    )


@router.patch("/{monitor_id}/rules/{rule_id}")
def update_rule(
    monitor_id: str, rule_id: str, body: RuleUpdate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    patch = body.model_dump(exclude_unset=True)
    return ApiResponse.ok(RuleService(container.metadata).update(ctx, monitor_id, rule_id, patch))


@router.delete("/{monitor_id}/rules/{rule_id}")
def delete_rule(
    monitor_id: str, rule_id: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    RuleService(container.metadata).delete(ctx, monitor_id, rule_id)
    return ApiResponse.ok({"deleted": rule_id})


@router.post("/{monitor_id}/rules:autogenerate")
def autogenerate(
    monitor_id: str, body: AutogenRequest, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[list[dict]]:
    """Generate suggested rules at the requested level from a table profile.

    For the scaffold the profile may be supplied in the request; in production it is
    computed by profiling the table via SQL pushdown.
    """
    profile = _profile_from_payload(body.profile) if body.profile else _demo_profile()
    specs = generate_rules(profile, body.level)
    svc = RuleService(container.metadata)
    created = [svc.create(ctx, monitor_id, spec) for spec in specs]
    return ApiResponse.ok(created, meta={"level": body.level, "count": len(created)})


@router.post("/{monitor_id}/tables/{table_ref}/rules:autogenerate-load-aware")
def autogenerate_load_aware(
    monitor_id: str, table_ref: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Generate the load-aware DQ rule pack for a table from its detected/confirmed load type
    (e.g. the SCD2 pack: active-unique, no-overlap, freshness + an active-count baseline metric).
    All rules are labelled origin='system'; idempotent — re-running only adds what's new and never
    overwrites a user-edited rule."""
    out = RuleService(container.metadata).autogenerate_load_aware(ctx, monitor_id, table_ref)
    return ApiResponse.ok(out, meta={"created": len(out["created"]), "skipped": len(out["skipped"])})


def _profile_from_payload(payload: dict) -> TableProfile:
    cols = [ColumnProfile(**c) for c in payload.get("columns", [])]
    return TableProfile(
        table=payload.get("table", "unknown"),
        row_count=payload.get("row_count", 0),
        columns=cols,
        load_ts_column=payload.get("load_ts_column"),
        detected_key=payload.get("detected_key", []),
    )


def _demo_profile() -> TableProfile:
    return TableProfile(
        table="DEMO",
        row_count=1000,
        load_ts_column="LOADED_AT",
        detected_key=["ID"],
        columns=[
            ColumnProfile(name="ID", data_type="NUMERIC", null_rate=0.0, is_key_candidate=True),
            ColumnProfile(name="EMAIL", data_type="STRING", null_rate=0.05, looks_like_email=True),
            ColumnProfile(
                name="AMOUNT", data_type="NUMERIC", null_rate=0.0, min_value=0, max_value=1000
            ),
        ],
    )
