"""Ingest a dbt run_results artifact. Failed tests and model errors become incidents."""

from __future__ import annotations

from fastapi import APIRouter

from dvp.api.deps import ContainerDep, ContextDep
from dvp.api.schemas import DbtRunIngest
from dvp.core.api_response import ApiResponse
from dvp.observability.dbt_ingest import DbtIngestService

router = APIRouter(prefix="/dbt", tags=["dbt"])


@router.post("/runs")
def ingest_run(body: DbtRunIngest, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Record one dbt invocation. A failed node opens one incident. A pass resolves it."""
    return ApiResponse.ok(
        DbtIngestService(container.metadata).ingest(ctx, body.model_dump())
    )
