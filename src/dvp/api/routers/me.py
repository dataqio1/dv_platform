from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request

from dvp.api.deps import ContainerDep, ContextDep, accessible_groups
from dvp.core.api_response import ApiResponse
from dvp.db.models import User

router = APIRouter(prefix="/me", tags=["identity"])


@router.get("/preferences")
def get_preferences(ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """The caller's saved UI preferences (last project, last section, …)."""
    with container.metadata.session(ctx) as s:
        user = s.get(User, ctx.user_id)
        return ApiResponse.ok(dict(user.preferences or {}) if user else {})


@router.put("/preferences")
def set_preferences(body: dict, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Merge-update the caller's UI preferences and return the full set. Scoped to the
    authenticated user, so preferences follow them across browsers/devices."""
    with container.metadata.session(ctx) as s:
        user = s.get(User, ctx.user_id)
        merged = {**(user.preferences or {}), **(body or {})}
        user.preferences = merged
        return ApiResponse.ok(merged)


@router.get("")
def whoami(ctx: ContextDep) -> ApiResponse[dict]:
    """The caller's identity in the active tenant (group + role)."""
    return ApiResponse.ok(
        {
            "user_id": ctx.user_id,
            "org_id": ctx.org_id,
            "group_id": ctx.group_id,
            "role": ctx.role,
            # Lets the UI gate capabilities (e.g. show project create/delete only to admins).
            "permissions": sorted(p.value for p in ctx.permissions),
        }
    )


@router.get("/groups")
def my_groups(
    request: Request,
    container: ContainerDep,
    authorization: Annotated[str | None, Header()] = None,
) -> ApiResponse[list[dict]]:
    """Tenant groups the caller may access (one per LDAP group). Use a group_id here as the
    `X-Group-Id` header on subsequent requests to act within that tenant."""
    return ApiResponse.ok(accessible_groups(request, container, authorization))
