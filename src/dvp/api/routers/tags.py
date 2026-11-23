from __future__ import annotations

from fastapi import APIRouter, Response, status

from dvp.api.deps import ContainerDep, ContextDep
from dvp.api.schemas import TagCreate, TagUpdate
from dvp.core.api_response import ApiResponse
from dvp.services.tags import TagService

router = APIRouter(prefix="/tags", tags=["tags"])


def _svc(container) -> TagService:  # noqa: ANN001
    return TagService(container.metadata)


@router.post("", status_code=status.HTTP_201_CREATED)
def create(body: TagCreate, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(
        _svc(container).create(ctx, name=body.name, color=body.color, description=body.description)
    )


@router.get("")
def list_tags(ctx: ContextDep, container: ContainerDep) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(_svc(container).list(ctx))


@router.patch("/{tag_id}")
def update(
    tag_id: str, body: TagUpdate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(
        _svc(container).update(
            ctx, tag_id, name=body.name, color=body.color, description=body.description
        )
    )


@router.delete("/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(tag_id: str, ctx: ContextDep, container: ContainerDep) -> Response:
    _svc(container).delete(ctx, tag_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
