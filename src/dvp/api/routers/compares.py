from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Response, status

from dvp.api.deps import ContainerDep, ContextDep
from dvp.api.schemas import (
    CompareBatchCreate,
    CompareCreate,
    CompareUpdate,
    DetectKeysRequest,
    DryRunCompare,
    RunCompareRequest,
    SampleByKey,
    ScheduleRequest,
    SuggestMappingRequest,
    TagAssignment,
)
from dvp.core.api_response import ApiResponse
from dvp.reporting.service import ReportService
from dvp.testing_engine.service import CompareService

router = APIRouter(prefix="/compares", tags=["data-testing"])


def _svc(container) -> CompareService:  # noqa: ANN001
    svc = CompareService(
        container.metadata, container.results, container.secrets, container.spark,
        cancel_registry=container.cancel_registry,
    )
    # How many compares run in parallel — surfaced by the queue bell so "waiting" makes sense.
    svc._concurrency = getattr(container.settings, "max_concurrent_runs", None)
    # Shared "live run" set — the in-process pool clears each id on finish; the bell reads it.
    # Only meaningful in thread mode; subprocess mode tracks the queue in the DB, so leave it
    # unset there and the bell falls back to the job status.
    svc.active_runs = (
        getattr(container, "active_runs", None)
        if getattr(container.settings, "compare_executor", "thread") != "subprocess"
        else None
    )
    return svc


def _enqueue(container, svc, ctx, job_id: str, variables: dict) -> None:  # noqa: ANN001
    """Hand a compare to the worker tier. 'subprocess' mode marks the job queued in the DB
    (a `dvp worker` runs it as its own process); 'thread' mode uses the in-process pool."""
    if container.settings.compare_executor == "subprocess":
        from dvp.workers.queue import JobQueueStore

        JobQueueStore(container.metadata).enqueue(ctx, job_id, variables)
    else:
        # Mark queued synchronously (before the pool picks it up) so the bell shows it waiting
        # while all worker slots are busy, and track it as a live run (cleared when it finishes).
        # The scheduler admits it only when its memory + vCPU needs fit the free pool.
        mem, cpu = svc.mark_queued(ctx, job_id)
        container.active_runs.add(job_id)
        container.jobs.submit(svc.run_safe, ctx, job_id, variables, memory_gb=mem, vcpus=cpu)


def _export_response(content, media: str, filename: str) -> Response:  # noqa: ANN001
    body = content if isinstance(content, bytes) else content.encode("utf-8")
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create(body: CompareCreate, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(
        _svc(container).create_job(
            ctx,
            kind=body.kind,
            name=body.name,
            source=body.source,
            target=body.target,
            options=body.options,
            batch_id=body.batch_id,
            batch_name=body.batch_name,
            project_id=body.project_id,
            source_system=body.source_system,
            layer=body.layer,
            tags=body.tags,
        )
    )


@router.patch("/{job_id}")
def update(
    job_id: str, body: CompareUpdate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Edit a test case's definition (name / kind / source / target / options). Only the fields
    present in the request body are changed; run history and schedule are preserved."""
    return ApiResponse.ok(
        _svc(container).update_job(ctx, job_id, body.model_dump(exclude_unset=True))
    )


@router.patch("/{job_id}/assignment")
def assign(
    job_id: str, body: TagAssignment, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Set this test case's project / source system / layer / tags."""
    return ApiResponse.ok(
        _svc(container).assign(
            ctx,
            job_id,
            project_id=body.project_id,
            source_system=body.source_system,
            layer=body.layer,
            tags=body.tags,
        )
    )


@router.get("")
def list_jobs(ctx: ContextDep, container: ContainerDep) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(_svc(container).list_jobs(ctx))


@router.get("/active")
def active_jobs(ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """In-flight work — running + queued compares — plus the worker pool's resource usage, for
    the queue bell."""
    data = _svc(container).active_jobs(ctx)
    if hasattr(container.jobs, "stats"):
        data["pool"] = container.jobs.stats()
    return ApiResponse.ok(data)


@router.get("/runs")
def list_runs(
    ctx: ContextDep, container: ContainerDep, project_id: str | None = Query(None)
) -> ApiResponse[list[dict]]:
    """Execution history — every run (not just the latest), newest first. Batch pairs grouped."""
    return ApiResponse.ok(_svc(container).list_runs(ctx, project_id))


@router.get("/runs/{run_id}/logs")
def run_logs(run_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[list[dict]]:
    """The execution trace of one specific historical run."""
    return ApiResponse.ok(_svc(container).get_run_logs(ctx, run_id))


@router.post("/batch", status_code=status.HTTP_201_CREATED)
def run_batch(
    body: CompareBatchCreate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Compare up to 100 table pairs in one batch (continue-on-error → partial)."""
    return ApiResponse.ok(
        _svc(container).run_batch(
            ctx,
            kind=body.kind,
            name=body.name,
            source=body.source,
            target=body.target,
            tables=body.tables,
            table_pairs=body.table_pairs,
            options=body.options,
            variables=body.variables,
            project_id=body.project_id,
        )
    )


@router.post("/batch:submit", status_code=status.HTTP_202_ACCEPTED)
def submit_batch(
    body: CompareBatchCreate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Create up to 100 compares and enqueue them all on the worker tier (async). Returns
    the batch id immediately; runs drain under the pool's concurrency cap so the API and
    other tenants are never blocked."""
    svc = _svc(container)
    batch_id, job_ids = svc.create_batch(
        ctx,
        kind=body.kind,
        name=body.name,
        source=body.source,
        target=body.target,
        tables=body.tables,
        table_pairs=body.table_pairs,
        options=body.options,
        project_id=body.project_id,
    )
    brid = str(uuid.uuid4())  # one batch-run id shared by every pair of this execution
    for jid in job_ids:
        _enqueue(container, svc, ctx, jid, {**(body.variables or {}), "__batch_run_id__": brid})
    return ApiResponse.ok({"batch_id": batch_id, "queued": len(job_ids), "status": "queued"})


@router.get("/batches")
def list_batches(ctx: ContextDep, container: ContainerDep) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(_svc(container).list_batches(ctx))


@router.get("/batches/{batch_id}")
def get_batch(batch_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(_svc(container).get_batch(ctx, batch_id))


@router.post("/batches/{batch_id}:sign-off")
def sign_off_batch(batch_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Generate the cutover sign-off for a finished batch. Reopens from Reports."""
    rsvc = ReportService(
        container.metadata, container.results, container.secrets, container.notifier
    )
    return ApiResponse.ok(rsvc.sign_off_batch(ctx, batch_id))


@router.get("/batches/{batch_id}/export")
def export_batch(
    batch_id: str,
    ctx: ContextDep,
    container: ContainerDep,
    fmt: str = Query("pdf", description="pdf | html | csv | json"),
) -> Response:
    """Download a batch's results as a single document. PDF/HTML/CSV are rendered from the
    aggregated summaries (not the heavy cell-level detail), so the export stays bounded even
    for a 100-table batch."""
    _svc(container).get_batch(ctx, batch_id)  # 404s unless the batch is in the caller's group
    rsvc = ReportService(
        container.metadata, container.results, container.secrets, container.notifier
    )
    content, media = rsvc.export_adhoc(
        ctx,
        report_type="data_validation",
        spec={"batch_id": batch_id},
        name=f"batch {batch_id}",
        fmt=fmt,
    )
    return _export_response(content, media, f"batch_{batch_id}.{fmt}")


@router.post("/batches/{batch_id}:cancel")
def cancel_batch(batch_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Cancel every running/queued pair in a batch (stops the engine + aborts in-flight queries)."""
    return ApiResponse.ok(_svc(container).cancel_batch(ctx, batch_id))


@router.post("/batches/{batch_id}:rerun", status_code=status.HTTP_202_ACCEPTED)
def rerun_batch(batch_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Re-run every pair in a batch asynchronously (reusing each job's stored variables).
    Returns immediately; the pairs execute on the worker tier."""
    svc = _svc(container)
    jobs = svc.requeue_batch(ctx, batch_id)  # 404s unless the batch is in the caller's group
    brid = str(uuid.uuid4())  # one batch-run id shared by every pair of this rerun
    for jid, variables in jobs:
        _enqueue(container, svc, ctx, jid, {**(variables or {}), "__batch_run_id__": brid})
    return ApiResponse.ok({"batch_id": batch_id, "queued": len(jobs), "status": "queued"})


@router.delete("/batches/{batch_id}", status_code=status.HTTP_200_OK)
def delete_batch(batch_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Delete every test case in a batch and its stored results + detail artifacts."""
    return ApiResponse.ok(_svc(container).delete_batch(ctx, batch_id))


@router.post("/suggest-mapping")
def suggest_mapping(
    body: SuggestMappingRequest, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Preview auto-detected key columns + source→target column mapping (with method and
    confidence) so the user can confirm/adjust before running a compare across tables whose
    column names differ."""
    return ApiResponse.ok(
        _svc(container).suggest_mapping(
            ctx, source=body.source, target=body.target,
            sample_size=body.sample_size, detect_key=body.detect_key, mode=body.mode,
        )
    )


@router.post("/detect-keys")
def detect_keys(
    body: DetectKeysRequest, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Detect candidate key columns for a dataset on demand (samples data — may be slow). Kept
    separate from column mapping so expanding a pair stays fast."""
    return ApiResponse.ok(
        _svc(container).detect_keys(ctx, source=body.source, target=body.target)
    )


@router.post("/sample-by-key")
def sample_by_key(
    body: SampleByKey, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Pick ~`limit` keys from the source, then fetch those keys' rows from both source and target
    (aligned by key) so the mapping UI can show sample data side by side. When `key_values` is
    given, look up exactly those user-provided keys instead of sampling. Works for table / SQL /
    file sides alike."""
    return ApiResponse.ok(
        _svc(container).sample_by_key(
            ctx, body.source, body.target,
            key=body.key, key_values=body.key_values, columns=body.columns,
            column_mappings=body.column_mappings, limit=body.limit,
        )
    )


@router.post("/dry-run")
def dry_run(
    body: DryRunCompare, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Run the real compare on a small sample (default ~1,000 keys) WITHOUT persisting — a fast,
    cheap check of the key/mapping/tolerances before committing to a full run."""
    return ApiResponse.ok(
        _svc(container).dry_run_compare(
            ctx, body.source, body.target,
            key=body.key, columns=body.columns,
            column_mappings=body.column_mappings, limit=body.limit,
        )
    )


@router.get("/{job_id}/failure-trend")
def failure_trend(
    job_id: str, ctx: ContextDep, container: ContainerDep, param_hash: str | None = None
) -> ApiResponse[dict]:
    """Failure-growth over time for this test case, scoped to one parameter set. Returns the latest
    failed-record count, the +/- change vs the previous run (abs + %), a direction (up=worse), and
    the actual-value series for the hover chart. `param_hash` picks the set (default: latest run's)."""
    return ApiResponse.ok(_svc(container).failure_trend(ctx, job_id, param_hash=param_hash))


@router.get("/{job_id}")
def get(job_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(_svc(container).get_job(ctx, job_id))


@router.post("/{job_id}:run")
def run(
    job_id: str, ctx: ContextDep, container: ContainerDep, body: RunCompareRequest | None = None
) -> ApiResponse[dict]:
    """Execute the compare synchronously; optional `variables` fill {{var}} placeholders."""
    return ApiResponse.ok(_svc(container).run(ctx, job_id, body.variables if body else {}))


@router.put("/{job_id}/schedule")
def set_schedule(
    job_id: str, body: ScheduleRequest, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Set (or clear with null/empty) a recurring cron schedule on a test case. For a batch pair,
    the schedule applies to the whole batch so it re-runs as a unit."""
    return ApiResponse.ok(_svc(container).set_schedule(ctx, job_id, body.schedule_cron))


@router.post("/{job_id}:submit", status_code=status.HTTP_202_ACCEPTED)
def submit(
    job_id: str, ctx: ContextDep, container: ContainerDep, body: RunCompareRequest | None = None
) -> ApiResponse[dict]:
    """Enqueue the compare on the worker tier and return immediately (202). The heavy work
    runs off the API path on a bounded pool, so many test cases never block the app. Poll
    GET /compares/{id} for status (queued → running → succeeded/failed)."""
    svc = _svc(container)
    variables = body.variables if body else {}
    _enqueue(container, svc, ctx, job_id, variables)
    return ApiResponse.ok({"job_id": job_id, "status": "queued"})


@router.get("/{job_id}/logs")
def logs(job_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[list[dict]]:
    """The execution trace (timestamped phases, engine, timing, warnings, outcome) of the
    compare's most recent run."""
    return ApiResponse.ok(_svc(container).get_logs(ctx, job_id))


@router.get("/{job_id}/logs/export")
def logs_export(job_id: str, ctx: ContextDep, container: ContainerDep) -> Response:
    """Download the execution log as a plain-text file."""
    lines = _svc(container).get_logs(ctx, job_id)
    body = "\n".join(
        f"[{ln.get('ts', '')}] [+{ln.get('elapsed_ms', 0)}ms] [{str(ln.get('level', 'info')).upper()}] "
        f"{ln.get('msg', '')}"
        for ln in lines
    )
    return _export_response(body or "(no log recorded)", "text/plain", f"execution_{job_id}.log")


@router.post("/{job_id}:cancel")
def cancel(job_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Cancel a running/queued compare — flags it (the engine stops between queries) and aborts
    the in-flight warehouse query. Returns the job in its 'cancelled' state."""
    return ApiResponse.ok(_svc(container).cancel_job(ctx, job_id))


@router.get("/{job_id}/variables")
def job_variables(job_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[list[str]]:
    """Variable names this test case references (so the UI can prompt for values)."""
    return ApiResponse.ok(_svc(container).variables_for(ctx, job_id))


@router.get("/{job_id}/differences")
def differences(
    job_id: str,
    ctx: ContextDep,
    container: ContainerDep,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    bucket: str | None = Query(
        None, description="match|value_difference|source_only|target_only|ambiguous"
    ),
) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(
        _svc(container).read_differences(ctx, job_id, offset=offset, limit=limit, bucket=bucket)
    )


@router.get("/{job_id}/differences/export")
def export_differences(job_id: str, ctx: ContextDep, container: ContainerDep) -> Response:
    """Download every captured difference as CSV (bucket, key, column, source, target) — the full
    stored set, not just the on-screen page."""
    csv_text = _svc(container).export_differences_csv(ctx, job_id)
    return _export_response(csv_text, "text/csv", f"differences_{job_id}.csv")


@router.post("/projects/{project_id}:rerun", status_code=status.HTTP_202_ACCEPTED)
def rerun_project(
    project_id: str,
    ctx: ContextDep,
    container: ContainerDep,
    only_failed: bool = Query(True, description="rerun only failed/differing test cases"),
) -> ApiResponse[dict]:
    """Bulk-rerun a project's test cases on the worker tier (async). By default only the unhealthy
    ones (failed or found differences); pass only_failed=false to rerun everything. Batch pairs are
    grouped so each batch re-runs as one unit."""
    svc = _svc(container)
    picked = svc.select_project_reruns(ctx, project_id, only_failed=only_failed)
    batch_run: dict[str, str] = {}
    for item in picked:
        extra = {}
        if item["batch_id"]:
            extra["__batch_run_id__"] = batch_run.setdefault(item["batch_id"], str(uuid.uuid4()))
        _enqueue(container, svc, ctx, item["job_id"], {**item["variables"], **extra})
    batches = {i["batch_id"] for i in picked if i["batch_id"]}
    singles = sum(1 for i in picked if not i["batch_id"])
    return ApiResponse.ok(
        {"project_id": project_id, "queued": len(picked), "batches": len(batches), "test_cases": singles + len(batches)}
    )


@router.get("/{job_id}/export")
def export_job(
    job_id: str,
    ctx: ContextDep,
    container: ContainerDep,
    fmt: str = Query("pdf", description="pdf | html | csv | json"),
) -> Response:
    """Download a single test case's result as a document (summary-level)."""
    _svc(container).get_job(ctx, job_id)  # 404s unless the job is in the caller's group
    rsvc = ReportService(
        container.metadata, container.results, container.secrets, container.notifier
    )
    content, media = rsvc.export_adhoc(
        ctx,
        report_type="data_validation",
        spec={"job_ids": [job_id]},
        name=f"test case {job_id}",
        fmt=fmt,
    )
    return _export_response(content, media, f"compare_{job_id}.{fmt}")


@router.delete("/{job_id}", status_code=status.HTTP_200_OK)
def delete(job_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Delete a test case and its stored result + detail artifacts (cascade cleanup)."""
    return ApiResponse.ok(_svc(container).delete_job(ctx, job_id))
