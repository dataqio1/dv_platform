"""Group settings that decide where customer data is allowed to land."""

from __future__ import annotations

from fastapi import APIRouter

from dvp.api.deps import ContainerDep, ContextDep
from dvp.api.schemas import OrgSettingsUpdate, SampleStoreUpdate
from dvp.core.api_response import ApiResponse
from dvp.services.org_settings import OrgSettingsService
from dvp.services.sample_location import SampleLocationService

router = APIRouter(prefix="/settings", tags=["admin"])


def _svc(container) -> SampleLocationService:  # noqa: ANN001
    return SampleLocationService(container.metadata)


def _org(container) -> OrgSettingsService:  # noqa: ANN001
    return OrgSettingsService(container.metadata)


@router.get("/sample-store")
def get_sample_store(ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(_svc(container).get(ctx))


@router.put("/sample-store")
def set_sample_store(
    body: SampleStoreUpdate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(
        _svc(container).set(
            ctx,
            connection_id=body.connection_id,
            database=body.database,
            schema=body.schema_name,
            table=body.table,
        )
    )


@router.delete("/sample-store")
def clear_sample_store(ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(_svc(container).clear(ctx))


# ----------------------------- organization / auth / governance -----------------------------
@router.get("/organization")
def get_organization(ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Org identity settings (name, logo, default timezone, …) as a JSON blob."""
    return ApiResponse.ok(_org(container).get(ctx, "organization"))


@router.put("/organization")
def set_organization(
    body: OrgSettingsUpdate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(_org(container).set(ctx, "organization", body.value))


@router.get("/auth")
def get_auth(ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Authentication / session policy (SSO, session TTL, MFA requirement, …)."""
    return ApiResponse.ok(_org(container).get(ctx, "auth"))


@router.put("/auth")
def set_auth(body: OrgSettingsUpdate, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(_org(container).set(ctx, "auth", body.value))


@router.get("/governance")
def get_governance(ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Governance policy (alert-silence defaults, deletion & audit retention)."""
    return ApiResponse.ok(_org(container).get(ctx, "governance"))


@router.put("/governance")
def set_governance(
    body: OrgSettingsUpdate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(_org(container).set(ctx, "governance", body.value))


@router.get("/compute")
def get_compute(ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Worker-tier resources the scheduler packs jobs into. Returns the admin override (if any)
    plus the LIVE pool: effective total memory / vCPUs (auto-detected unless overridden) and what's
    in use right now."""
    data = _org(container).get(ctx, "compute")
    if hasattr(container.jobs, "stats"):
        data["pool"] = container.jobs.stats()
    return ApiResponse.ok(data)


@router.put("/compute")
def set_compute(
    body: OrgSettingsUpdate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    """Set the worker pool's total memory (GB) and max vCPUs — applied to the live scheduler so
    the new ceilings take effect immediately (no restart)."""
    saved = _org(container).set(ctx, "compute", body.value)
    v = body.value or {}
    if hasattr(container.jobs, "set_limits"):
        container.jobs.set_limits(
            total_memory_gb=float(v["total_memory_gb"]) if v.get("total_memory_gb") else None,
            max_vcpus=int(v["max_vcpus"]) if v.get("max_vcpus") else None,
        )
    if hasattr(container.jobs, "stats"):
        saved["pool"] = container.jobs.stats()
    return ApiResponse.ok(saved)
