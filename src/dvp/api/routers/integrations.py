"""Outbound integrations admin — tenant notification channels + routing rules.

Channels (email/slack/teams/webhook) and the routes that fire them on events. Per-tenant,
gated on MANAGE_GROUP. Secrets go to the SecretStore and are never returned.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Response, status

from dvp.api.deps import ContainerDep, ContextDep
from dvp.api.schemas import IntegrationCreate, IntegrationUpdate, RouteCreate, RouteUpdate
from dvp.core.api_response import ApiResponse
from dvp.integrations import specs
from dvp.services.integrations import IntegrationService, RouteService

router = APIRouter(prefix="/integrations", tags=["admin"])


def _isvc(container) -> IntegrationService:  # noqa: ANN001
    return IntegrationService(
        container.metadata, container.secrets, container.settings, container.notifier
    )


def _rsvc(container) -> RouteService:  # noqa: ANN001
    return RouteService(container.metadata)


# --- literals first (declared before /{integration_id} patterns) ---
@router.get("/provider-specs")
def provider_specs() -> ApiResponse[dict]:
    """Extensible channel catalog (labels + per-provider fields + which are secret)."""
    return ApiResponse.ok(specs.provider_specs())


@router.get("/routes")
def list_routes(ctx: ContextDep, container: ContainerDep) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(_rsvc(container).list(ctx))


@router.post("/routes", status_code=status.HTTP_201_CREATED)
def create_route(body: RouteCreate, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(
        _rsvc(container).create(
            ctx, event=body.event, min_severity=body.min_severity,
            integration_id=body.integration_id, enabled=body.enabled,
        )
    )


@router.patch("/routes/{route_id}")
def update_route(
    route_id: str, body: RouteUpdate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(
        _rsvc(container).update(ctx, route_id, min_severity=body.min_severity, enabled=body.enabled)
    )


@router.delete("/routes/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_route(route_id: str, ctx: ContextDep, container: ContainerDep) -> Response:
    _rsvc(container).delete(ctx, route_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- integrations ---
@router.get("")
def list_integrations(ctx: ContextDep, container: ContainerDep) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(_isvc(container).list(ctx))


@router.post("", status_code=status.HTTP_201_CREATED)
def create_integration(
    body: IntegrationCreate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(
        _isvc(container).create(ctx, provider=body.provider, name=body.name, values=body.values, enabled=body.enabled)
    )


@router.patch("/{integration_id}")
def update_integration(
    integration_id: str, body: IntegrationUpdate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(
        _isvc(container).update(ctx, integration_id, name=body.name, enabled=body.enabled, values=body.values)
    )


@router.delete("/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_integration(
    integration_id: str, ctx: ContextDep, container: ContainerDep
) -> Response:
    _isvc(container).delete(ctx, integration_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{integration_id}:test")
def test_integration(
    integration_id: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(_isvc(container).test(ctx, integration_id))


@router.get("/{integration_id}/deliveries")
def integration_deliveries(
    integration_id: str, ctx: ContextDep, container: ContainerDep,
    limit: int = Query(default=20, ge=1, le=100),
) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(_isvc(container).deliveries(ctx, integration_id, limit))
