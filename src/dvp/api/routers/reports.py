from __future__ import annotations

from fastapi import APIRouter, Query, Response, status

from dvp.api.deps import ContainerDep, ContextDep
from dvp.api.schemas import ReportCreate
from dvp.core.api_response import ApiResponse
from dvp.reporting.service import ReportService

router = APIRouter(prefix="/reports", tags=["reports"])


def _svc(container) -> ReportService:  # noqa: ANN001
    return ReportService(
        container.metadata, container.results, container.secrets, container.notifier
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create(body: ReportCreate, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    return ApiResponse.ok(
        _svc(container).create_report(
            ctx,
            name=body.name,
            report_type=body.report_type,
            project_id=body.project_id,
            spec=body.spec,
            schedule_cron=body.schedule_cron,
            delivery=body.delivery,
        )
    )


@router.get("")
def list_reports(ctx: ContextDep, container: ContainerDep) -> ApiResponse[list[dict]]:
    return ApiResponse.ok(_svc(container).list_reports(ctx))


@router.post("/{report_id}:generate")
def generate(report_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """(Re)build the report document from current executions and persist it."""
    return ApiResponse.ok(_svc(container).generate(ctx, report_id))


@router.get("/{report_id}/versions")
def list_versions(
    report_id: str, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[list[dict]]:
    """Immutable snapshots of this report, newest first."""
    return ApiResponse.ok(_svc(container).list_versions(ctx, report_id))


@router.get("/{report_id}/versions/{version}")
def get_version(
    report_id: str, version: int, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(_svc(container).get_version(ctx, report_id, version))


@router.get("/{report_id}/trend")
def trend(report_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Pass-rate trend across versions + delta of latest vs previous (regression signal)."""
    return ApiResponse.ok(_svc(container).trend(ctx, report_id))


@router.post("/{report_id}:deliver")
def deliver(report_id: str, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Regenerate + deliver the report digest to its configured channels now (email/Slack)."""
    return ApiResponse.ok(_svc(container).deliver(ctx, report_id, regenerate=True))


@router.get("/{report_id}")
def get(
    report_id: str,
    ctx: ContextDep,
    container: ContainerDep,
    document: bool = Query(True, description="include the full generated document"),
) -> ApiResponse[dict]:
    return ApiResponse.ok(_svc(container).get_report(ctx, report_id, include_document=document))


@router.get("/{report_id}/export")
def export(
    report_id: str,
    ctx: ContextDep,
    container: ContainerDep,
    fmt: str = Query("json", description="json | csv | html"),
) -> Response:
    content, media = _svc(container).export(ctx, report_id, fmt)
    filename = f"report_{report_id}.{fmt}"
    return Response(
        content=content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(report_id: str, ctx: ContextDep, container: ContainerDep) -> Response:
    _svc(container).delete_report(ctx, report_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
