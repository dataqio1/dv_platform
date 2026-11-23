from __future__ import annotations

from fastapi import APIRouter, Body, Query, status

from dvp.api.deps import ContainerDep, ContextDep
from dvp.api.schemas import ConnectionCreate, FileDatasetVerifyRequest
from dvp.core.api_response import ApiResponse, Warning_
from dvp.services.catalog import CatalogService
from dvp.services.connections import ConnectionService
from dvp.services.file_datasets import FileDatasetService

router = APIRouter(prefix="/connections", tags=["connections"])


def _lift(result: dict) -> ApiResponse[dict]:
    warnings = [Warning_(**w) for w in result.pop("warnings", [])]
    return ApiResponse.with_warnings(result, warnings) if warnings else ApiResponse.ok(result)


@router.post("", status_code=status.HTTP_201_CREATED)
def create(body: ConnectionCreate, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    svc = ConnectionService(container.metadata, container.secrets)
    data = svc.create(
        ctx,
        source_type=body.source_type,
        name=body.name,
        coordinates=body.coordinates,
        auth_method=body.auth_method,
        secret=body.secret,
        allowed_scopes=body.allowed_scopes,
        metadata_columns=body.metadata_columns,
        metadata_convention_ids=body.metadata_convention_ids,
    )
    return ApiResponse.ok(data)


@router.get("")
def list_connections(ctx: ContextDep, container: ContainerDep) -> ApiResponse[list[dict]]:
    svc = ConnectionService(container.metadata, container.secrets)
    return ApiResponse.ok(svc.list(ctx))


@router.get("/{connection_id}")
def get(connection_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    svc = ConnectionService(container.metadata, container.secrets)
    return ApiResponse.ok(svc.get(ctx, connection_id))


@router.get("/{connection_id}/databases")
def list_databases(
    connection_id: str,
    ctx: ContextDep,
    container: ContainerDep,
    refresh: bool = Query(False, description="bypass + repopulate the cache"),
) -> ApiResponse[dict]:
    """List the databases the connection's role can read (account-wide for Snowflake;
    the single coordinates.database for single-database sources). Cached; pass refresh=true
    to re-fetch from the source."""
    return _lift(
        CatalogService(container.metadata, container.secrets).list_databases(
            ctx, connection_id, refresh=refresh
        )
    )


@router.get("/{connection_id}/schemas")
def list_schemas(
    connection_id: str,
    ctx: ContextDep,
    container: ContainerDep,
    database: str | None = Query(None),
    refresh: bool = Query(False, description="bypass + repopulate the cache"),
) -> ApiResponse[dict]:
    """List schemas in a database (defaults to the connection's database; graceful fallback).
    Cached; pass refresh=true to re-fetch from the source."""
    return _lift(
        CatalogService(container.metadata, container.secrets).list_schemas(
            ctx, connection_id, database, refresh=refresh
        )
    )


@router.get("/{connection_id}/tables")
def list_catalog_tables(
    connection_id: str,
    ctx: ContextDep,
    container: ContainerDep,
    schema: str = Query(...),
    database: str | None = Query(None),
    refresh: bool = Query(False, description="bypass + repopulate the cache"),
) -> ApiResponse[dict]:
    """List tables & views in a schema (optionally within a specific database). Cached; pass
    refresh=true to re-fetch from the source."""
    return _lift(
        CatalogService(container.metadata, container.secrets).list_tables(
            ctx, connection_id, schema, database, refresh=refresh
        )
    )


@router.post("/{connection_id}/catalog:refresh")
def refresh_catalog_cache(
    connection_id: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Clear every cached catalog listing for this connection and re-fetch its schemas.
    Table lists repopulate lazily on next access."""
    return _lift(
        CatalogService(container.metadata, container.secrets).refresh_cache(ctx, connection_id)
    )


@router.post("/{connection_id}:detect-load-type-async")
def detect_load_type_async(
    connection_id: str, body: dict, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Start a BACKGROUND load-type detection over a connection + scope (works before a monitor
    exists — the create flow). Returns a run id immediately; poll
    GET /monitors/load-type-detections/{run_id} for progress + streaming per-table results."""
    from dvp.services.load_type_detector import LoadTypeDetectorService

    svc = LoadTypeDetectorService(container.metadata, container.secrets)
    run = svc.start(ctx, connection_id=connection_id, scope=body.get("scope") or {})
    if hasattr(container, "jobs") and hasattr(container.jobs, "submit"):
        container.jobs.submit(svc.run_detection, ctx, run["id"])
    else:
        svc.run_detection(ctx, run["id"])
        run = svc.get(ctx, run["id"])
    return ApiResponse.ok(run)


@router.get("/{connection_id}/columns")
def list_columns(
    connection_id: str,
    ctx: ContextDep,
    container: ContainerDep,
    schema: str = Query(...),
    table: str = Query(...),
    database: str | None = Query(None),
) -> ApiResponse[dict]:
    """List a table's/view's columns (name, data_type, nullable) for per-table config."""
    return _lift(
        CatalogService(container.metadata, container.secrets).list_columns(
            ctx, connection_id, schema, table, database
        )
    )


@router.get("/{connection_id}/rowcount")
def row_count(
    connection_id: str,
    ctx: ContextDep,
    container: ContainerDep,
    schema: str = Query(...),
    table: str = Query(...),
    database: str | None = Query(None),
) -> ApiResponse[dict]:
    """Approximate row count for a table (warehouse metadata estimate, else COUNT(*))."""
    return _lift(
        CatalogService(container.metadata, container.secrets).row_count(
            ctx, connection_id, schema, table, database
        )
    )


@router.get("/{connection_id}/sample")
def sample(
    connection_id: str,
    ctx: ContextDep,
    container: ContainerDep,
    schema: str = Query(...),
    table: str = Query(...),
    database: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
) -> ApiResponse[dict]:
    """A few preview rows (columns + rows) from a table/view."""
    return _lift(
        CatalogService(container.metadata, container.secrets).sample(
            ctx, connection_id, schema, table, database, limit
        )
    )


@router.post("/{connection_id}/sql-preview")
def sql_preview(
    connection_id: str,
    ctx: ContextDep,
    container: ContainerDep,
    sql: str = Body(..., embed=True),
    database: str | None = Body(None, embed=True),
    limit: int = Body(20, embed=True, ge=1, le=100),
) -> ApiResponse[dict]:
    """Validate an arbitrary SQL dataset by running it (capped) — returns columns + sample rows,
    or a warning carrying the DB error if the query is invalid."""
    return _lift(
        CatalogService(container.metadata, container.secrets).preview_sql(
            ctx, connection_id, sql, database, limit
        )
    )


@router.get("/{connection_id}/file-datasets")
def list_file_datasets(
    connection_id: str,
    ctx: ContextDep,
    container: ContainerDep,
    pattern: str | None = Query(None, description="prefix/substring/glob filter on dataset name"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=500),
    include_verified: bool = Query(True),
    include_blocked: bool = Query(True),
) -> ApiResponse[dict]:
    """Discover candidate file datasets in an S3/file connection with verification status +
    pagination. Each dataset carries a schema hash (for drift) and verified/blocked/
    unverified/outdated state from persisted TableMetadata. Per-dataset schema-fetch failures
    surface as warnings."""
    return _lift(
        FileDatasetService(container.metadata, container.secrets).list_datasets(
            ctx,
            connection_id,
            pattern=pattern,
            page=page,
            size=size,
            include_verified=include_verified,
            include_blocked=include_blocked,
        )
    )


@router.post("/{connection_id}/file-datasets:verify")
def verify_file_datasets(
    connection_id: str,
    body: FileDatasetVerifyRequest,
    ctx: ContextDep,
    container: ContainerDep,
) -> ApiResponse[dict]:
    """Bulk-verify selected file datasets — fetch + hash each schema and persist its
    verification status in TableMetadata (so the verified/blocked state is real and sticks)."""
    return _lift(
        FileDatasetService(container.metadata, container.secrets).verify(
            ctx, connection_id, body.datasets
        )
    )


@router.post("/{connection_id}:test")
def test(connection_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    svc = ConnectionService(container.metadata, container.secrets)
    result = svc.test(ctx, connection_id)
    warnings = (
        []
        if result["ok"]
        else [Warning_(code=f"connection_{result['category']}", message=result["detail"])]
    )
    return ApiResponse.with_warnings(result, warnings)


@router.delete("/{connection_id}", status_code=status.HTTP_200_OK)
def delete(connection_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    svc = ConnectionService(container.metadata, container.secrets)
    svc.delete(ctx, connection_id)
    return ApiResponse.ok({"deleted": connection_id})
