from __future__ import annotations

from fastapi import APIRouter, Query, status

from dvp.api.deps import ContainerDep, ContextDep
from dvp.api.schemas import FeedbackCreate, FeedbackUpdate
from dvp.core.api_response import ApiResponse
from dvp.services.feedback import FeedbackService

router = APIRouter(prefix="/feedback", tags=["feedback"])


def _svc(container) -> FeedbackService:  # noqa: ANN001
    return FeedbackService(container.metadata)


@router.post("", status_code=status.HTTP_201_CREATED)
def submit(body: FeedbackCreate, ctx: ContextDep, container: ContainerDep) -> ApiResponse[dict]:
    """Any signed-in user submits feedback from any page; the widget captures page + context."""
    return ApiResponse.ok(
        _svc(container).create(
            ctx,
            message=body.message,
            category=body.category,
            rating=body.rating,
            page=body.page,
            url=body.url,
            user_agent=body.user_agent,
            context=body.context,
        )
    )


@router.get("")
def list_feedback(
    ctx: ContextDep,
    container: ContainerDep,
    status_filter: str | None = Query(None, alias="status"),
) -> ApiResponse[list[dict]]:
    """Admin review (MANAGE_GROUP). Optional `?status=new|reviewed|closed`."""
    return ApiResponse.ok(_svc(container).list(ctx, status=status_filter))


@router.patch("/{feedback_id}")
def update_status(
    feedback_id: str, body: FeedbackUpdate, ctx: ContextDep, container: ContainerDep
) -> ApiResponse[dict]:
    return ApiResponse.ok(_svc(container).set_status(ctx, feedback_id, body.status))
