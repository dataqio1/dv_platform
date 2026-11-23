from __future__ import annotations

from fastapi import APIRouter, status

from dvp.api.deps import ContainerDep, ContextDep
from dvp.api.schemas import ProjectCreate, ProjectUpdate
from dvp.core.api_response import ApiResponse
from dvp.services.projects import ProjectService

router = APIRouter(prefix="/projects", tags=["projects"])


def _svc(container) -> ProjectService:  # noqa: ANN001
    return ProjectService(container.metadata)


@router.post("", status_code=status.HTTP_201_CREATED)
def create(body: ProjectCreate, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(
        _svc(container).create(
            ctx, name=body.name, description=body.description, tags=body.tags,
            variables=body.variables,
        )
    )


@router.get("")
def list_projects(
    ctx: ContextDep, container: ContainerDep, include_archived: bool = False
) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(_svc(container).list(ctx, include_archived=include_archived))


@router.get("/{project_id}")
def get(project_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(_svc(container).get(ctx, project_id))


@router.patch("/{project_id}")
def update(
    project_id: str, body: ProjectUpdate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(
        _svc(container).update(
            ctx, project_id, name=body.name, description=body.description, tags=body.tags,
            variables=body.variables,
        )
    )


@router.get("/{project_id}/test-cases")
def test_cases(project_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(_svc(container).test_cases(ctx, project_id))


@router.delete("/{project_id}")
def delete(project_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Soft-delete (archive) a project. Admin-only; test cases are retained and restorable."""
    return ApiResponse.ok(_svc(container).delete(ctx, project_id))


@router.post("/{project_id}:restore")
def restore(project_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Un-archive a soft-deleted project. Admin-only (MANAGE_PROJECTS)."""
    return ApiResponse.ok(_svc(container).restore(ctx, project_id))
