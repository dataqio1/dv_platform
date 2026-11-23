"""Record table lineage. The table page walks it past one hop."""

from __future__ import annotations

from fastapi import APIRouter

from dvp.api.deps import ContainerDep, ContextDep
from dvp.api.schemas import LineageEdges
from dvp.core.api_response import ApiResponse
from dvp.observability.lineage import LineageService

router = APIRouter(prefix="/lineage", tags=["lineage"])


@router.post("/edges")
def add_edges(body: LineageEdges, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(
        LineageService(container.metadata).add_edges(
            ctx, [edge.model_dump() for edge in body.edges]
        )
    )
