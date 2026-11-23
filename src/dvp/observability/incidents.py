"""IncidentService — first-class incidents with fingerprint dedup, auto-resolve, and
evidence on every alert.

Design goals (from the competitive analysis — alert fatigue is the #1 reason teams
abandon observability tools):
* **Dedup**: a recurring signal updates one incident (bumps `occurrences`) instead of
  spamming new ones, keyed by a stable `dedup_key`.
* **Auto-resolve**: when the underlying signal clears, the incident resolves itself.
* **Evidence**: every incident carries the metric/value/expected-band/sample that
  triggered it, so an alert is immediately actionable and trustworthy.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_, select

from dvp.core.errors import NotFoundError, ValidationError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.base import utcnow
from dvp.db.models import Incident, IncidentComment, User
from dvp.db.session import MetadataStore
from dvp.services import audit

ACTIVE = ("open", "acknowledged")
# Triage dispositions a user can set when resolving (FR: mark an alert okay or false).
RESOLUTIONS = ("ok", "false_positive", "confirmed")


class IncidentService:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def open_or_update(
        self,
        ctx: TenantContext,
        *,
        monitor_id: str | None,
        dedup_key: str,
        kind: str,
        dimension: str,
        severity: str,
        summary: str,
        evidence: dict | None = None,
        table_ref: str | None = None,
        dq_rule_id: str | None = None,
    ) -> dict:
        ctx.require(Permission.WRITE)
        is_new = False
        suppressed = False
        with self.store.session(ctx) as s:
            existing = s.scalars(
                select(Incident).where(
                    Incident.group_id == ctx.group_id,
                    Incident.dedup_key == dedup_key,
                    Incident.status.in_(ACTIVE),
                )
            ).first()
            if existing is not None:
                existing.occurrences = (existing.occurrences or 1) + 1
                existing.severity = severity
                existing.summary = summary
                existing.evidence = evidence or {}
                existing.updated_at = utcnow()
                result = _dict(existing)
            else:
                inc = Incident(
                    org_id=ctx.org_id,
                    group_id=ctx.group_id,
                    monitor_id=monitor_id,
                    dq_rule_id=dq_rule_id,
                    table_ref=table_ref,
                    severity=severity,
                    status="open",
                    summary=summary,
                    dedup_key=dedup_key,
                    dimension=dimension,
                    kind=kind,
                    evidence=evidence or {},
                    occurrences=1,
                )
                s.add(inc)
                s.flush()
                result = _dict(inc)
                is_new = True
                # An active monitor-scoped silence suppresses alerting for brand-new incidents
                # on that monitor (UI silence scope = "broader"). The incident is still recorded.
                if monitor_id is not None and self._monitor_silenced(s, ctx, monitor_id):
                    suppressed = True
        # Notify configured channels only when a NEW incident opens (occurrence bumps on an
        # existing incident don't re-alert) and no broader silence is in effect. Best-effort —
        # never breaks incident creation.
        if is_new and not suppressed:
            from dvp.integrations import dispatch

            dispatch.notify_incident_opened(ctx, result)
        return result

    @staticmethod
    def _monitor_silenced(s, ctx: TenantContext, monitor_id: str) -> bool:  # noqa: ANN001
        return (
            s.scalars(
                select(Incident).where(
                    Incident.group_id == ctx.group_id,
                    Incident.monitor_id == monitor_id,
                    Incident.silence_scope == "monitor",
                    Incident.silenced_until > utcnow(),
                )
            ).first()
            is not None
        )

    def attach_compare(self, ctx: TenantContext, incident_id: str, compare: dict) -> dict:
        """Record a compare result on an incident. Status is left for a person to resolve."""
        ctx.require(Permission.WRITE)
        from sqlalchemy.orm.attributes import flag_modified

        with self.store.session(ctx) as s:
            inc = self._load(s, ctx, incident_id)
            evidence = dict(inc.evidence or {})
            evidence["compare"] = compare
            inc.evidence = evidence
            flag_modified(inc, "evidence")
            return _dict(inc)

    def resolve_by_key(self, ctx: TenantContext, dedup_key: str) -> int:
        """Auto-resolve any active incident whose signal has cleared. Returns count."""
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            rows = s.scalars(
                select(Incident).where(
                    Incident.group_id == ctx.group_id,
                    Incident.dedup_key == dedup_key,
                    Incident.status.in_(ACTIVE),
                )
            ).all()
            for inc in rows:
                inc.status = "resolved"
                inc.resolved_at = utcnow()
            return len(rows)

    def list(
        self,
        ctx: TenantContext,
        *,
        status: str | None = None,
        monitor_id: str | None = None,
        table_ref: str | None = None,
        active_only: bool = False,
    ) -> list[dict]:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            q = select(Incident).where(Incident.group_id == ctx.group_id)
            if monitor_id:
                q = q.where(Incident.monitor_id == monitor_id)
            if table_ref:
                q = q.where(Incident.table_ref == table_ref)
            if status:
                q = q.where(Incident.status == status)
            if active_only:
                q = q.where(or_(Incident.status == "open", Incident.status == "acknowledged"))
            q = q.order_by(Incident.updated_at.desc())
            return [_dict(i) for i in s.scalars(q).all()]

    def get(self, ctx: TenantContext, incident_id: str) -> dict:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            return _dict(self._load(s, ctx, incident_id))

    def set_status(self, ctx: TenantContext, incident_id: str, status: str) -> dict:
        ctx.require(Permission.WRITE)
        if status not in ("open", "acknowledged", "resolved"):
            raise NotFoundError("invalid status", details={"status": status})
        with self.store.session(ctx) as s:
            inc = self._load(s, ctx, incident_id)
            inc.status = status
            inc.resolved_at = utcnow() if status == "resolved" else None
            audit.record(s, ctx, f"incident.{status}", entity_type="incident", entity_id=incident_id)
            return _dict(inc)

    def resolve(
        self, ctx: TenantContext, incident_id: str, *, resolution: str, note: str | None = None
    ) -> dict:
        """Resolve with a disposition — 'ok' (valid but accepted), 'false_positive', or
        'confirmed' — recording who and when. An optional note is stored as a comment."""
        ctx.require(Permission.WRITE)
        if resolution not in RESOLUTIONS:
            raise ValidationError(
                "invalid resolution", details={"resolution": resolution, "allowed": RESOLUTIONS}
            )
        with self.store.session(ctx) as s:
            inc = self._load(s, ctx, incident_id)
            inc.status = "resolved"
            inc.resolution = resolution
            inc.resolved_by = ctx.user_id
            inc.resolved_at = utcnow()
            if note and note.strip():
                self._add_comment(s, ctx, incident_id, note.strip())
            audit.record(
                s, ctx, "incident.resolve", entity_type="incident", entity_id=incident_id,
                detail={"resolution": resolution},
            )
            return _dict(inc)

    def reopen(self, ctx: TenantContext, incident_id: str) -> dict:
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            inc = self._load(s, ctx, incident_id)
            inc.status = "open"
            inc.resolution = None
            inc.resolved_by = None
            inc.resolved_at = None
            audit.record(s, ctx, "incident.reopen", entity_type="incident", entity_id=incident_id)
            return _dict(inc)

    # ----------------------------- silence (suppress alerting) -----------------------------
    def silence(
        self,
        ctx: TenantContext,
        incident_id: str,
        *,
        until: str | datetime | None = None,
        duration_hours: float | None = None,
        scope: str = "incident",
        note: str | None = None,
        ticket_url: str | None = None,
    ) -> dict:
        """Suppress alerting for this incident until `until` (ISO string or datetime) or for
        `duration_hours` from now. Records who/why/ticket for the audit trail. Exactly one of
        `until` / `duration_hours` must resolve to a future time."""
        ctx.require(Permission.WRITE)
        if scope not in ("incident", "monitor"):
            raise ValidationError(
                "invalid silence scope",
                details={"scope": scope, "allowed": ("incident", "monitor")},
            )
        deadline = self._resolve_deadline(until, duration_hours)
        with self.store.session(ctx) as s:
            inc = self._load(s, ctx, incident_id)
            inc.silenced_until = deadline
            inc.silenced_by = ctx.user_id
            inc.silence_scope = scope
            inc.silence_note = (note or "").strip() or None
            inc.ticket_url = (ticket_url or "").strip() or None
            if note and note.strip():
                self._add_comment(s, ctx, incident_id, f"Silenced: {note.strip()}")
            audit.record(
                s, ctx, "incident.silence", entity_type="incident", entity_id=incident_id,
                detail={"until": deadline.isoformat(), "scope": scope, "ticket_url": inc.ticket_url},
            )
            return _dict(inc)

    def unsilence(self, ctx: TenantContext, incident_id: str) -> dict:
        """Lift a silence so the incident alerts again."""
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            inc = self._load(s, ctx, incident_id)
            inc.silenced_until = None
            inc.silenced_by = None
            inc.silence_scope = None
            inc.silence_note = None
            audit.record(
                s, ctx, "incident.unsilence", entity_type="incident", entity_id=incident_id
            )
            return _dict(inc)

    @staticmethod
    def _resolve_deadline(until: str | datetime | None, duration_hours: float | None) -> datetime:
        from datetime import timedelta

        if until is not None:
            dt = until if isinstance(until, datetime) else _parse_dt(until)
            if dt is None:
                raise ValidationError("invalid 'until' timestamp", details={"until": str(until)})
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            deadline = dt
        elif duration_hours is not None:
            if duration_hours <= 0:
                raise ValidationError(
                    "duration_hours must be positive", details={"duration_hours": duration_hours}
                )
            deadline = utcnow() + timedelta(hours=duration_hours)
        else:
            raise ValidationError("one of 'until' or 'duration_hours' is required")
        if deadline <= utcnow():
            raise ValidationError(
                "silence deadline must be in the future", details={"until": deadline.isoformat()}
            )
        return deadline

    # ----------------------------- comments (who / when) -----------------------------
    def add_comment(self, ctx: TenantContext, incident_id: str, body: str) -> dict:
        ctx.require(Permission.WRITE)
        if not (body or "").strip():
            raise ValidationError("comment body is required")
        with self.store.session(ctx) as s:
            self._load(s, ctx, incident_id)  # authorize + existence
            c = self._add_comment(s, ctx, incident_id, body.strip())
            s.flush()
            audit.record(
                s, ctx, "incident.comment", entity_type="incident", entity_id=incident_id
            )
            cid = c.id
        return self._comment_dict_by_id(ctx, cid)

    def comments(self, ctx: TenantContext, incident_id: str) -> list[dict]:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            self._load(s, ctx, incident_id)
            rows = s.execute(
                select(IncidentComment, User)
                .outerjoin(User, User.id == IncidentComment.user_id)
                .where(IncidentComment.incident_id == incident_id)
                .order_by(IncidentComment.ts.asc())
            ).all()
            return [_comment_dict(c, u) for (c, u) in rows]

    def _add_comment(self, s, ctx, incident_id: str, body: str) -> IncidentComment:  # noqa: ANN001
        c = IncidentComment(
            org_id=ctx.org_id, group_id=ctx.group_id, incident_id=incident_id,
            user_id=ctx.user_id, body=body, ts=utcnow(),
        )
        s.add(c)
        return c

    def _comment_dict_by_id(self, ctx: TenantContext, comment_id: str) -> dict:
        with self.store.session(ctx) as s:
            row = s.execute(
                select(IncidentComment, User)
                .outerjoin(User, User.id == IncidentComment.user_id)
                .where(IncidentComment.id == comment_id)
            ).first()
            return _comment_dict(row[0], row[1]) if row else {}

    @staticmethod
    def _load(s, ctx, incident_id):  # noqa: ANN001
        inc = s.get(Incident, incident_id)
        if inc is None or inc.group_id != ctx.group_id:
            raise NotFoundError("incident not found", details={"id": incident_id})
        return inc


def _dict(i: Incident) -> dict:
    return {
        "id": i.id,
        "monitor_id": i.monitor_id,
        "dq_rule_id": i.dq_rule_id,
        "table_ref": i.table_ref,
        "severity": i.severity,
        "status": i.status,
        "summary": i.summary,
        "dimension": i.dimension,
        "kind": i.kind,
        "evidence": i.evidence,
        "occurrences": i.occurrences,
        "created_at": i.created_at.isoformat() if i.created_at else None,
        "updated_at": i.updated_at.isoformat() if i.updated_at else None,
        "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
        "resolution": i.resolution,
        "resolved_by": i.resolved_by,
        "silenced": _is_silenced(i),
        "silenced_until": i.silenced_until.isoformat() if i.silenced_until else None,
        "silenced_by": i.silenced_by,
        "silence_scope": i.silence_scope,
        "silence_note": i.silence_note,
        "ticket_url": i.ticket_url,
    }


def _is_silenced(i: Incident) -> bool:
    return i.silenced_until is not None and i.silenced_until > utcnow()


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _comment_dict(c: IncidentComment, u: User | None) -> dict:
    return {
        "id": c.id,
        "incident_id": c.incident_id,
        "body": c.body,
        "user_id": c.user_id,
        "author": (u.display_name or u.email) if u else None,
        "author_email": u.email if u else None,
        "ts": c.ts.isoformat() if c.ts else None,
    }
