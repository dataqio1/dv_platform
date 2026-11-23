"""Dispatch events (P1a: incident_opened) to a tenant's notification channels.

Registered process-wide at app startup. IncidentService calls `notify_incident_opened`
after a NEW incident commits (occurrence bumps don't re-notify — natural dedup). Delivery is
best-effort and fully guarded: a notification failure is recorded but never breaks the
incident. Each attempt is logged as a NotificationDelivery (history + per-incident dedup).
"""

from __future__ import annotations

from sqlalchemy import select

from dvp.core.logging import get_logger
from dvp.db.base import utcnow
from dvp.db.models import Integration, NotificationDelivery, NotificationRoute
from dvp.integrations.providers import build_channel_provider
from dvp.integrations.specs import severity_rank
from dvp.services import audit

log = get_logger("dvp.integrations.dispatch")

_dispatcher: NotificationDispatcher | None = None


def set_dispatcher(d: NotificationDispatcher | None) -> None:
    global _dispatcher
    _dispatcher = d


def notify_incident_opened(ctx, incident: dict) -> None:  # noqa: ANN001
    """Module hook called from IncidentService — no-op if no dispatcher is registered
    (e.g. unit tests that don't boot the app). Never raises."""
    d = _dispatcher
    if d is None:
        return
    try:
        d.notify_incident_opened(ctx, incident)
    except Exception as exc:  # noqa: BLE001 - notifications must never break incident creation
        log.warning("incident_notify_failed", error=str(exc))


class NotificationDispatcher:
    def __init__(self, container) -> None:  # noqa: ANN001
        self.store = container.metadata
        self.secrets = container.secrets
        self.notifier = container.notifier
        self.settings = container.settings

    def notify_incident_opened(self, ctx, incident: dict) -> None:  # noqa: ANN001
        event = "incident_opened"
        entity_id = incident.get("id")
        sev = incident.get("severity", "warning")
        subject = f"[{str(sev).upper()}] {incident.get('summary') or 'Data incident'}"
        body = _render(incident)

        with self.store.session(ctx) as s:
            routes = s.scalars(
                select(NotificationRoute).where(
                    NotificationRoute.group_id == ctx.group_id,
                    NotificationRoute.event == event,
                    NotificationRoute.enabled,
                )
            ).all()
            done: set[str] = set()
            for route in routes:
                if severity_rank(sev) < severity_rank(route.min_severity):
                    continue
                integ = s.get(Integration, route.integration_id)
                if integ is None or not integ.enabled or integ.category != "channel":
                    continue
                if integ.id in done:
                    continue
                done.add(integ.id)
                dup = s.scalar(
                    select(NotificationDelivery).where(
                        NotificationDelivery.group_id == ctx.group_id,
                        NotificationDelivery.integration_id == integ.id,
                        NotificationDelivery.event == event,
                        NotificationDelivery.entity_id == entity_id,
                    )
                )
                if dup is not None:
                    continue
                self._deliver(s, ctx, integ, event, incident, subject, body)

    def _deliver(self, s, ctx, integ, event, incident, subject, body) -> None:  # noqa: ANN001
        secret = (
            self.secrets.get(ctx, integ.secret_ref)
            if (integ.secret_ref and self.secrets)
            else None
        )
        try:
            provider = build_channel_provider(
                integ.provider, integ.config, secret,
                local=self.settings.is_local, notifier=self.notifier,
            )
            result = provider.send(
                subject,
                body,
                context={
                    "incident_id": incident.get("id"),
                    "severity": incident.get("severity"),
                    "table_ref": incident.get("table_ref"),
                    "kind": incident.get("kind"),
                },
            )
            ok, detail = result.ok, result.detail
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"provider error: {exc}"
        status = "sent" if ok else "failed"
        s.add(
            NotificationDelivery(
                org_id=ctx.org_id, group_id=ctx.group_id, integration_id=integ.id,
                event=event, entity_type="incident", entity_id=incident.get("id"),
                status=status, detail={"subject": subject, "detail": detail}, ts=utcnow(),
            )
        )
        integ.last_delivery_at = utcnow()
        integ.status = "active" if ok else "error"
        integ.last_error = None if ok else detail
        audit.record(
            s, ctx, f"notification.{status}", entity_type="integration", entity_id=integ.id,
            detail={"event": event, "incident": incident.get("id"), "detail": detail},
        )


def _render(incident: dict) -> str:
    ev = incident.get("evidence") or {}
    lines = [str(incident.get("summary") or "Data incident")]
    if incident.get("table_ref"):
        lines.append(f"Table: {incident['table_ref']}")
    if incident.get("dimension"):
        lines.append(f"Dimension: {incident['dimension']}")
    if incident.get("kind"):
        lines.append(f"Kind: {incident['kind']}")
    if ev:
        lines.append("Evidence: " + ", ".join(f"{k}={v}" for k, v in list(ev.items())[:6]))
    return "\n".join(lines)
