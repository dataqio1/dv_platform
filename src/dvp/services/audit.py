"""Append-only audit logging helper (docs/design/02 §2, 07)."""

from __future__ import annotations

import csv
import io
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from dvp.core.errors import ValidationError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.base import utcnow
from dvp.db.models import AuditLog, User
from dvp.db.session import MetadataStore


def record(
    s: Session,
    ctx: TenantContext,
    action: str,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    detail: dict | None = None,
) -> None:
    s.add(
        AuditLog(
            org_id=ctx.org_id,
            group_id=ctx.group_id,
            user_id=ctx.user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail or {},
            ts=utcnow(),
        )
    )


def list_entries(
    store: MetadataStore,
    ctx: TenantContext,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    user_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Newest first. Scoped to the caller's group so one tenant cannot read another's trail.

    Filters (all optional, AND-combined): entity_type/entity_id, exact `action`, `user_id`
    (the actor), and a `since`/`until` ISO-8601 time window. The audit screen drives these.
    """
    ctx.require(Permission.READ)
    limit = max(1, min(limit, 200))
    with store.session(ctx) as s:
        rows = _query(
            s, ctx, entity_type=entity_type, entity_id=entity_id, action=action,
            user_id=user_id, since=since, until=until, limit=limit,
        )
        return _with_actors(s, rows)


def export_csv(
    store: MetadataStore,
    ctx: TenantContext,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    user_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 10000,
) -> str:
    """Same filters as list_entries, rendered as CSV for the audit screen's Export button."""
    ctx.require(Permission.READ)
    limit = max(1, min(limit, 50000))
    with store.session(ctx) as s:
        rows = _query(
            s, ctx, entity_type=entity_type, entity_id=entity_id, action=action,
            user_id=user_id, since=since, until=until, limit=limit,
        )
        entries = _with_actors(s, rows)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ts", "action", "entity_type", "entity_id", "user_id", "actor", "detail"])
    for e in entries:
        writer.writerow([
            e["ts"], e["action"], e["entity_type"] or "", e["entity_id"] or "",
            e["user_id"] or "", e["actor"] or "", _json(e["detail"]),
        ])
    return buf.getvalue()


def _query(s, ctx, *, entity_type, entity_id, action, user_id, since, until, limit):  # noqa: ANN001
    q = select(AuditLog).where(AuditLog.group_id == ctx.group_id)
    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)
    if entity_id:
        q = q.where(AuditLog.entity_id == entity_id)
    if action:
        q = q.where(AuditLog.action == action)
    if user_id:
        q = q.where(AuditLog.user_id == user_id)
    since_dt = _parse_dt(since, "since")
    until_dt = _parse_dt(until, "until")
    if since_dt is not None:
        q = q.where(AuditLog.ts >= since_dt)
    if until_dt is not None:
        q = q.where(AuditLog.ts <= until_dt)
    return list(s.scalars(q.order_by(AuditLog.ts.desc()).limit(limit)))


def _with_actors(s, rows) -> list[dict]:  # noqa: ANN001
    ids = {r.user_id for r in rows if r.user_id}
    actors: dict[str, User] = {}
    if ids:
        for user in s.scalars(select(User).where(User.id.in_(ids))):
            actors[user.id] = user
    return [_entry(r, actors.get(r.user_id) if r.user_id else None) for r in rows]


def _parse_dt(value: str | None, field: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"invalid '{field}' timestamp", details={field: value}) from exc


def _json(detail: dict) -> str:
    import json

    try:
        return json.dumps(detail, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(detail)


def _entry(row: AuditLog, user: User | None) -> dict:
    return {
        "id": row.id,
        "action": row.action,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "detail": row.detail or {},
        "ts": row.ts.isoformat() if row.ts else None,
        "user_id": row.user_id,
        "actor": (user.display_name or user.email) if user else None,
        "actor_email": user.email if user else None,
    }
