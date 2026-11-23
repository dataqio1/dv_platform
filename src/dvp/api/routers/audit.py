"""Read the append-only audit trail for the caller's group."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

from dvp.api.deps import ContainerDep, ContextDep
from dvp.core.api_response import ApiResponse
from dvp.services import audit

router = APIRouter(prefix="/audit", tags=["audit"])


def _default_window(since: str | None, entity_id: str | None) -> str | None:
    """Guard against unbounded scans: a broad query (no entity and no explicit lower bound)
    defaults to the last 24h. Entity-specific trails stay unbounded."""
    if since is None and entity_id is None:
        return (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    return since


@router.get("")
def list_audit(
    ctx: ContextDep,
    container: ContainerDep,
    entity_type: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    user_id: str | None = None,
    since: str | None = Query(default=None, description="ISO-8601 lower bound (inclusive)"),
    until: str | None = Query(default=None, description="ISO-8601 upper bound (inclusive)"),
    limit: int = Query(default=50, ge=1, le=200),
) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(
        audit.list_entries(
            container.metadata,
            ctx,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            user_id=user_id,
            since=_default_window(since, entity_id),
            until=until,
            limit=limit,
        )
    )


@router.get("/export", response_class=PlainTextResponse)
def export_audit(
    ctx: ContextDep,
    container: ContainerDep,
    entity_type: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    user_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = Query(default=10000, ge=1, le=50000),
) -> PlainTextResponse:
    """CSV export of the filtered audit trail (audit screen's Export button)."""
    csv_text = audit.export_csv(
        container.metadata,
        ctx,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        user_id=user_id,
        since=_default_window(since, entity_id),
        until=until,
        limit=limit,
    )
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="audit-log.csv"'},
    )
