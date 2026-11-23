from __future__ import annotations

from fastapi import APIRouter, Response, status

from dvp.api.deps import ContainerDep, ContextDep
from dvp.api.schemas import TableMetadataInject, TableMetadataItem
from dvp.core.api_response import ApiResponse
from dvp.services.table_metadata import (
    LOAD_PATTERNS,
    TableMetadataService,
    classify_pattern,
    metadata_presets,
    pattern_specs,
    rule_suggestions,
)

router = APIRouter(prefix="/table-metadata", tags=["table-metadata"])


@router.get("/rule-suggestions")
def get_rule_suggestions(load_pattern: str) -> ApiResponse[list[dict]]:
    """Suggested baseline rules for a load pattern — the user acknowledges & tweaks before adding."""
    return ApiResponse.ok(rule_suggestions(load_pattern))


def _svc(container) -> TableMetadataService:  # noqa: ANN001
    return TableMetadataService(container.metadata)


@router.put("", status_code=status.HTTP_200_OK)
def inject(
    body: TableMetadataInject, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Inject/override load-pattern metadata for many tables (idempotent, keyed by table_ref).
    This is the integration surface for a customer's custom ingestion software — call it with
    `source` set to your system's name; it upserts and records provenance."""
    items = [i.model_dump(exclude_none=False) for i in body.items]
    return ApiResponse.ok(_svc(container).bulk_upsert(ctx, items, source=body.source))


@router.get("")
def list_metadata(ctx: ContextDep, container: ContainerDep) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(_svc(container).list(ctx))


@router.get("/patterns")
def patterns() -> ApiResponse[list[str]]:
    """The supported load patterns (for pickers)."""
    return ApiResponse.ok(list(LOAD_PATTERNS))


@router.get("/pattern-specs")
def pattern_spec_catalog() -> ApiResponse[dict]:
    """Extensible load-pattern catalog: each pattern's label, description, and the per-table
    fields it needs — drives the creation UI so new patterns need no frontend change."""
    return ApiResponse.ok(pattern_specs())


@router.get("/metadata-presets")
def get_metadata_presets() -> ApiResponse[dict]:
    """The ETL metadata-column preset library (dbt/Fivetran/Airbyte/Stitch/Informatica/custom) +
    the tool-agnostic role catalog. Drives the 'declare your metadata columns' first step."""
    return ApiResponse.ok(metadata_presets())


@router.post("/classify")
def classify(
    body: dict,
    ctx: ContextDep,
    container: ContainerDep,  # noqa: ARG001
) -> ApiResponse[dict]:
    """Propose a load pattern from a column profile ({columns:[{name,data_type}], row_count,
    convention?}). `convention` is an optional role→column map (from the ETL preset) used
    convention-first. A suggestion only — confirm it by PUT-ing it back (or inject it)."""
    return ApiResponse.ok(
        classify_pattern(
            body.get("columns", []), body.get("row_count"), convention=body.get("convention")
        )
    )


@router.put("/{table_ref}")
def upsert_one(
    table_ref: str, body: TableMetadataItem, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    item = body.model_dump(exclude_none=False)
    item["table_ref"] = table_ref
    return ApiResponse.ok(_svc(container).upsert(ctx, item))


@router.get("/{table_ref}")
def get_one(table_ref: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(_svc(container).get(ctx, table_ref))


@router.delete("/{table_ref}", status_code=status.HTTP_204_NO_CONTENT)
def delete_one(table_ref: str, ctx: ContextDep, container: ContainerDep) -> Response:
    _svc(container).delete(ctx, table_ref)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
