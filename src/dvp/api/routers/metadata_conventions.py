from __future__ import annotations

from fastapi import APIRouter, Response, status

from dvp.api.deps import ContainerDep, ContextDep
from dvp.api.schemas import MetadataConventionCreate, MetadataConventionUpdate
from dvp.core.api_response import ApiResponse
from dvp.services.metadata_conventions import MetadataConventionService

router = APIRouter(prefix="/metadata-conventions", tags=["metadata-conventions"])


def _svc(container) -> MetadataConventionService:  # noqa: ANN001
    return MetadataConventionService(container.metadata)


@router.get("")
def list_conventions(ctx: ContextDep, container: ContainerDep) -> ApiResponse[list[dict]]:
    """The convention library: shipped `sys:` presets + this group's own, all attachable."""
    return ApiResponse.ok(_svc(container).list(ctx))


@router.post("", status_code=status.HTTP_201_CREATED)
def create(
    body: MetadataConventionCreate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(
        _svc(container).create(ctx, name=body.name, roles=body.roles, scd2=body.scd2)
    )


@router.get("/{conv_id}")
def get(conv_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(_svc(container).get(ctx, conv_id))


@router.put("/{conv_id}")
def update(
    conv_id: str, body: MetadataConventionUpdate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(_svc(container).update(ctx, conv_id, body.model_dump(exclude_none=True)))


@router.delete("/{conv_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(conv_id: str, ctx: ContextDep, container: ContainerDep) -> Response:
    _svc(container).delete(ctx, conv_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
