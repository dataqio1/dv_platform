"""Group membership admin — list members, add, change role, remove.

Operates on the caller's active tenant (the resolved ctx.group_id); the {group_id} in the
path must match it. Cross-group administration is an org-level concern (future).
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from dvp.api.deps import ContainerDep, ContextDep
from dvp.api.schemas import MemberAdd, MemberRoleUpdate
from dvp.core.api_response import ApiResponse
from dvp.core.errors import ValidationError
from dvp.services.members import MembershipService

router = APIRouter(prefix="/groups", tags=["admin"])


def _svc(container) -> MembershipService:  # noqa: ANN001
    return MembershipService(container.metadata, container.settings)


def _assert_active(ctx, group_id: str) -> None:  # noqa: ANN001
    if group_id != ctx.group_id:
        raise ValidationError(
            "can only manage members of the active group",
            details={"active_group": ctx.group_id, "requested": group_id},
        )


@router.get("/{group_id}/members")
def list_members(group_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[list[dict]]:
    _assert_active(ctx, group_id)
    return ApiResponse.ok(_svc(container).list(ctx))


@router.post("/{group_id}/members", status_code=status.HTTP_201_CREATED)
def add_member(
    group_id: str, body: MemberAdd, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    _assert_active(ctx, group_id)
    return ApiResponse.ok(_svc(container).add(ctx, email=body.email, role=body.role))


@router.put("/{group_id}/members/{user_id}")
def update_member_role(
    group_id: str, user_id: str, body: MemberRoleUpdate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    _assert_active(ctx, group_id)
    return ApiResponse.ok(_svc(container).update_role(ctx, user_id, body.role))


@router.delete("/{group_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    group_id: str, user_id: str, ctx: ContextDep, container: ContainerDep
) -> Response:
    _assert_active(ctx, group_id)
    _svc(container).remove(ctx, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
