"""System info — runtime mode, identity provider, adapter backends, scheduler state.

Read-only and admin-gated. Lets the Admin UI adapt to the deployment (e.g. hide in-app role
editing under LDAP, hide real notification channels in local mode). Never exposes secrets or
full connection strings — only the backend *kind* / URL scheme.
"""

from __future__ import annotations

from fastapi import APIRouter

from dvp import __version__
from dvp.api.deps import ContainerDep, ContextDep
from dvp.core.api_response import ApiResponse
from dvp.core.tenancy import Permission

router = APIRouter(prefix="/system", tags=["admin"])


def _db_kind(settings) -> str:  # noqa: ANN001
    try:
        url = settings.effective_database_url
    except Exception:  # noqa: BLE001
        return "unknown"
    scheme = (url.split("://", 1)[0] or "").lower() if url else ""
    if scheme.startswith("postgresql"):
        return "postgres"
    if scheme.startswith("sqlite"):
        return "sqlite"
    return scheme or "unknown"


@router.get("/info")
def system_info(ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    ctx.require(Permission.MANAGE_GROUP)
    s = container.settings
    return ApiResponse.ok(
        {
            "version": __version__,
            "profile": str(s.profile),
            "is_local": s.is_local,
            "identity_provider": str(s.identity_provider),
            "secret_backend": str(s.secret_backend),
            "result_backend": str(s.result_backend),
            "database": _db_kind(s),
            "scheduler": {
                "enabled": s.scheduler_enabled,
                "interval_seconds": s.scheduler_interval_seconds,
            },
        }
    )
