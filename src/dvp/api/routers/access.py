"""People & access admin: the RBAC role→permission matrix and service-account API keys."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from dvp.api.deps import ContainerDep, ContextDep
from dvp.api.schemas import ServiceAccountCreate
from dvp.core.api_response import ApiResponse
from dvp.core.tenancy import Permission
from dvp.services.access import AccessService, role_matrix

router = APIRouter(prefix="/access", tags=["admin"])


@router.get("/roles")
def get_roles(ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Static role→permission matrix rendered by the People & access screen."""
    ctx.require(Permission.MANAGE_GROUP)
    return ApiResponse.ok(role_matrix())


@router.get("/service-accounts")
def list_service_accounts(ctx: ContextDep, container: ContainerDep) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(AccessService(container.metadata).list_service_accounts(ctx))


@router.post("/service-accounts", status_code=status.HTTP_201_CREATED)
def create_service_account(
    body: ServiceAccountCreate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Create a service account. The returned `api_key` is shown exactly once."""
    return ApiResponse.ok(
        AccessService(container.metadata).create_service_account(ctx, name=body.name, role=body.role)
    )


@router.post("/service-accounts/{service_account_id}:rotate")
def rotate_service_account_key(
    service_account_id: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Rotate the API key. The returned `api_key` is shown exactly once; the old key stops working."""
    return ApiResponse.ok(AccessService(container.metadata).rotate_key(ctx, service_account_id))


@router.delete("/service-accounts/{service_account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_service_account(
    service_account_id: str, ctx: ContextDep, container: ContainerDep
) -> Response:
    AccessService(container.metadata).delete_service_account(ctx, service_account_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
