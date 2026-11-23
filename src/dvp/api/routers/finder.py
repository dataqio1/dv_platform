from __future__ import annotations

from fastapi import APIRouter, Query

from dvp.api.deps import ContainerDep, ContextDep
from dvp.api.schemas import FindColumnIndexBuild, FindColumnIndexConfig, FindColumnRequest
from dvp.core.api_response import ApiResponse, Warning_
from dvp.services.finder import FinderService
from dvp.services.finder_index import FinderIndexService

router = APIRouter(prefix="/finder", tags=["finder"])


def _lift(result: dict) -> ApiResponse[dict]:
    warnings = [Warning_(**w) for w in result.pop("warnings", [])]
    return ApiResponse.with_warnings(result, warnings) if warnings else ApiResponse.ok(result)


@router.post("/search")
def search(body: FindColumnRequest, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Find which tables & columns contain the given value(s), with sample rows.

    Scope widens by omission: no connection → every readable connection; no schema → every
    schema. `match_mode="co_occurrence"` (>=2 values) returns only tables where every value
    appears in the SAME row. When a connection has a built index it's used automatically to skip
    non-matching tables; `use_index="never"` forces a live scan. Per-table failures come back as
    warnings, never a 500."""
    result = FinderService(container.metadata, container.secrets).search(
        ctx,
        connection_id=body.connection_id,
        connection_ids=body.connection_ids,
        values=body.values,
        schema=body.schema_name,
        schemas=body.schemas,
        database=body.database,
        tables=body.tables,
        match_mode=body.match_mode,
        sample_limit=body.sample_limit,
        max_tables=body.max_tables,
        use_index=body.use_index,
        confirm_concurrency=body.confirm_concurrency,
    )
    return _lift(result)


@router.get("/searches")
def list_searches(
    ctx: ContextDep, container: ContainerDep, limit: int = Query(50, ge=1, le=200)
) -> ApiResponse[list]:
    """This user's recent Find Column searches (most recent first) — value(s), scope, how much
    was scanned (tables/columns/connections), matches, and latency. Per-user, never shared."""
    return ApiResponse.ok(
        FinderService(container.metadata, container.secrets).list_searches(ctx, limit=limit)
    )


@router.get("/searches/{search_id}")
def get_search(
    search_id: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Re-open a past search with its full stored result (hits + sample rows) — no re-scan."""
    return ApiResponse.ok(
        FinderService(container.metadata, container.secrets).get_search(ctx, search_id)
    )


@router.delete("/searches/{search_id}")
def delete_search(
    search_id: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Delete one of the caller's past searches."""
    FinderService(container.metadata, container.secrets).delete_search(ctx, search_id)
    return ApiResponse.ok({"deleted": search_id})


@router.post("/index:build")
def build_index(
    body: FindColumnIndexBuild, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """(Re)build the Bloom index for a connection. Runs in the background when a job queue is
    available, else synchronously. Pass `incremental` + `watermark_column` to index only new rows."""
    svc = FinderIndexService(container.metadata, container.secrets)
    jobs = getattr(container, "jobs", None)
    if body.background and jobs is not None and hasattr(jobs, "submit"):
        jobs.submit(
            svc.build, ctx,
            connection_id=body.connection_id, schema=body.schema_name, tables=body.tables,
            watermark_column=body.watermark_column, incremental=body.incremental,
            max_text_len=body.max_text_len,
        )
        return ApiResponse.ok({"connection_id": body.connection_id, "status": "building"})
    return _lift(svc.build(
        ctx, connection_id=body.connection_id, schema=body.schema_name, tables=body.tables,
        watermark_column=body.watermark_column, incremental=body.incremental,
        max_text_len=body.max_text_len,
    ))


@router.get("/index/status")
def index_status(
    ctx: ContextDep, container: ContainerDep, connection_id: str = Query(...)
) -> ApiResponse[dict]:
    """Index coverage (tables/columns indexed, last scan), latest run, and schedule config."""
    return ApiResponse.ok(
        FinderIndexService(container.metadata, container.secrets).status(ctx, connection_id)
    )


@router.put("/index/config")
def set_index_config(
    body: FindColumnIndexConfig, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Set the refresh schedule for a connection's index (daily is picked up by the scheduler)."""
    return ApiResponse.ok(
        FinderIndexService(container.metadata, container.secrets).set_config(
            ctx, connection_id=body.connection_id, schema=body.schema_name, tables=body.tables,
            watermark_column=body.watermark_column, frequency=body.frequency, enabled=body.enabled,
        )
    )
