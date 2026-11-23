"""FastAPI dependencies: container access + per-request TenantContext."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request

from dvp.abstractions.factory import Container
from dvp.core.errors import AuthError
from dvp.core.tenancy import TenantContext
from dvp.services.tenancy import resolve_context


def get_container(request: Request) -> Container:
    return request.app.state.container


def accessible_groups(
    request: Request, container: Container, authorization: str | None
) -> list[dict]:
    """Authenticate and return the tenant groups the caller may access. For LDAP identities
    this JIT-provisions a tenant per LDAP group; for local/OIDC it's the bound group."""
    if container.identity is None:
        raise AuthError("identity provider is not configured")
    identity = container.identity.authenticate(authorization)
    if identity.groups:  # LDAP/SSO: each group is a tenant, provisioned on login
        from dvp.services.tenancy import GroupRoleMapper, TenantProvisioner

        mapper = GroupRoleMapper.from_settings(container.settings)
        groups = TenantProvisioner(container.metadata, mapper).sync(identity)
        if not groups:
            raise AuthError("user has no tenant access")
        return groups
    local = getattr(request.app.state, "local_tenant", None)
    if local is None:
        raise AuthError("no active tenant")
    return [
        {
            "user_id": local.user_id,
            "group_id": local.group_id,
            "org_id": identity.org_id or local.org_id,
            "name": "local",
            "role": "group_admin",
        }
    ]


def get_context(
    request: Request,
    container: Annotated[Container, Depends(get_container)],
    authorization: Annotated[str | None, Header()] = None,
    x_group_id: Annotated[str | None, Header()] = None,
) -> TenantContext:
    groups = accessible_groups(request, container, authorization)
    # Pick the active tenant: explicit X-Group-Id (must be one the caller belongs to),
    # else the only one, else require a choice.
    if x_group_id is not None:
        chosen = next((g for g in groups if g["group_id"] == x_group_id), None)
        if chosen is None:
            raise AuthError("not a member of the requested group")
    elif len(groups) == 1:
        chosen = groups[0]
    else:
        raise AuthError("multiple tenants available; pass X-Group-Id")

    return resolve_context(
        container.metadata,
        user_id=chosen["user_id"],  # the authenticated user, not just any member of the group
        org_id=chosen["org_id"],
        group_id=chosen["group_id"],
    )


ContainerDep = Annotated[Container, Depends(get_container)]
ContextDep = Annotated[TenantContext, Depends(get_context)]
