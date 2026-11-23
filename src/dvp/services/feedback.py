"""FeedbackService — in-app, per-page feedback.

Any authenticated user can submit feedback from any page (READ is enough — even report-only
personas should be able to tell us something's wrong). Admins (MANAGE_GROUP) review it. We store
the page + browser context so a report is actionable without a repro.
"""

from __future__ import annotations

from sqlalchemy import select

from dvp.core.errors import NotFoundError, ValidationError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.models import Feedback
from dvp.db.session import MetadataStore

_CATEGORIES = {"idea", "issue", "praise", "other"}
_STATUSES = {"new", "reviewed", "closed"}


def _dict(f: Feedback) -> dict:
    return {
        "id": f.id,
        "user_id": f.user_id,
        "category": f.category,
        "message": f.message,
        "rating": f.rating,
        "page": f.page,
        "url": f.url,
        "user_agent": f.user_agent,
        "context": f.context or {},
        "status": f.status,
        "created_at": f.created_at.isoformat() if f.created_at else None,
    }


class FeedbackService:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def create(
        self,
        ctx: TenantContext,
        *,
        message: str,
        category: str = "idea",
        rating: int | None = None,
        page: str | None = None,
        url: str | None = None,
        user_agent: str | None = None,
        context: dict | None = None,
    ) -> dict:
        ctx.require(Permission.READ)  # anyone signed in can give feedback
        if not (message or "").strip():
            raise ValidationError("feedback needs a message")
        cat = category if category in _CATEGORIES else "other"
        with self.store.session(ctx) as s:
            f = Feedback(
                org_id=ctx.org_id,
                group_id=ctx.group_id,
                user_id=ctx.user_id,
                category=cat,
                message=message.strip()[:8000],
                rating=rating,
                page=(page or None),
                url=(url or None),
                user_agent=(user_agent or None)[:512] if user_agent else None,
                context=context or None,
                status="new",
            )
            s.add(f)
            s.flush()
            return _dict(f)

    def list(self, ctx: TenantContext, *, status: str | None = None) -> list[dict]:
        ctx.require(Permission.MANAGE_GROUP)  # admin-only review
        with self.store.session(ctx) as s:
            q = select(Feedback).where(Feedback.group_id == ctx.group_id)
            if status in _STATUSES:
                q = q.where(Feedback.status == status)
            rows = s.scalars(q.order_by(Feedback.created_at.desc())).all()
            return [_dict(r) for r in rows]

    def set_status(self, ctx: TenantContext, feedback_id: str, status: str) -> dict:
        ctx.require(Permission.MANAGE_GROUP)
        if status not in _STATUSES:
            raise ValidationError("invalid status", details={"status": status})
        with self.store.session(ctx) as s:
            f = s.get(Feedback, feedback_id)
            if f is None or f.group_id != ctx.group_id:
                raise NotFoundError("feedback not found", details={"id": feedback_id})
            f.status = status
            s.flush()
            return _dict(f)
