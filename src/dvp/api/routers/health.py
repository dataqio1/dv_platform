from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from dvp import __version__
from dvp.api.deps import ContainerDep, ContextDep
from dvp.core.api_response import ApiResponse
from dvp.core.logging import get_logger

router = APIRouter(tags=["system"])
log = get_logger("dvp.health")


@router.get("/health")
def health(container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(
        {
            "status": "ok",
            "version": __version__,
            "profile": container.settings.profile,
            "rls_enabled": container.settings.rls_enabled,
        }
    )


@router.get("/ready")
def ready(container: ContainerDep) -> JSONResponse:
    """Readiness probe (distinct from /health liveness): verifies the metadata DB is reachable so
    an orchestrator only routes traffic once dependencies are actually up. Returns 200 when ready,
    503 otherwise — no auth, no config details, just per-dependency status."""
    checks: dict[str, str] = {}
    is_ready = True
    try:
        with container.metadata.session() as s:
            s.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:  # noqa: BLE001 — any failure means "not ready"; detail is logged, not exposed
        is_ready = False
        checks["database"] = "unavailable"
        log.warning("readiness_check_failed", check="database", exc_info=True)
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={"status": "ready" if is_ready else "not_ready", "checks": checks},
    )


@router.get("/whoami")
def whoami(ctx: ContextDep) -> ApiResponse[dict]:
    return ApiResponse.ok(
        {
            "user_id": ctx.user_id,
            "org_id": ctx.org_id,
            "group_id": ctx.group_id,
            "role": ctx.role,
            "permissions": sorted(p.value for p in ctx.permissions),
        }
    )
