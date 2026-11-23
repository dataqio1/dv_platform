"""IntegrationService + RouteService — tenant-configured outbound channels and routing.

Each group owns its own integrations (like connections): config in the DB, secrets in the
SecretStore (never returned to the client). Gated on MANAGE_GROUP; every mutation audited.
"""

from __future__ import annotations

from sqlalchemy import select

from dvp.abstractions.secret_store import SecretStore, secret_path
from dvp.core.errors import NotFoundError, ValidationError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.base import utcnow
from dvp.db.models import Integration, NotificationDelivery, NotificationRoute
from dvp.db.session import MetadataStore
from dvp.integrations import specs
from dvp.integrations.providers import build_channel_provider
from dvp.services import audit

_EVENTS = ("incident_opened",)


class IntegrationService:
    def __init__(self, store: MetadataStore, secrets: SecretStore, settings=None, notifier=None) -> None:  # noqa: ANN001
        self.store = store
        self.secrets = secrets
        self.settings = settings
        self.notifier = notifier

    # ----------------------------- read -----------------------------
    def list(self, ctx: TenantContext) -> list[dict]:
        ctx.require(Permission.MANAGE_GROUP)
        with self.store.session(ctx) as s:
            rows = s.scalars(
                select(Integration)
                .where(Integration.group_id == ctx.group_id)
                .order_by(Integration.name)
            ).all()
            return [_dict(r) for r in rows]

    def deliveries(self, ctx: TenantContext, integration_id: str, limit: int = 20) -> list[dict]:
        ctx.require(Permission.MANAGE_GROUP)
        with self.store.session(ctx) as s:
            rows = s.scalars(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.group_id == ctx.group_id,
                    NotificationDelivery.integration_id == integration_id,
                )
                .order_by(NotificationDelivery.ts.desc())
                .limit(limit)
            ).all()
            return [_delivery_dict(r) for r in rows]

    # ----------------------------- write -----------------------------
    def create(
        self, ctx: TenantContext, *, provider: str, name: str, values: dict, enabled: bool = True
    ) -> dict:
        ctx.require(Permission.MANAGE_GROUP)
        if not specs.known_provider(provider):
            raise ValidationError("unknown provider", details={"provider": provider})
        if not (name or "").strip():
            raise ValidationError("name is required")
        config, secret = _split(provider, values or {})
        with self.store.session(ctx) as s:
            integ = Integration(
                org_id=ctx.org_id, group_id=ctx.group_id, category="channel",
                provider=provider, name=name.strip(), enabled=enabled, config=config,
                status="active" if secret or config else "unconfigured", created_by=ctx.user_id,
            )
            s.add(integ)
            s.flush()
            if secret:
                ref = secret_path(ctx, integ.id)
                self.secrets.put(ctx, ref, secret)
                integ.secret_ref = ref
            audit.record(s, ctx, "integration.create", entity_type="integration", entity_id=integ.id,
                         detail={"provider": provider, "name": integ.name})
            return _dict(integ)

    def update(self, ctx: TenantContext, integration_id: str, *, name=None, enabled=None, values=None) -> dict:  # noqa: ANN001
        ctx.require(Permission.MANAGE_GROUP)
        with self.store.session(ctx) as s:
            integ = self._load(s, ctx, integration_id)
            if name is not None:
                integ.name = name.strip()
            if enabled is not None:
                integ.enabled = bool(enabled)
            if values:
                config, secret = _split(integ.provider, values)
                integ.config = {**(integ.config or {}), **config}
                if secret:  # only overwrite secret fields that were actually provided
                    ref = integ.secret_ref or secret_path(ctx, integ.id)
                    existing = self.secrets.get(ctx, ref) if integ.secret_ref else {}
                    self.secrets.put(ctx, ref, {**existing, **secret})
                    integ.secret_ref = ref
            audit.record(s, ctx, "integration.update", entity_type="integration", entity_id=integ.id)
            return _dict(integ)

    def delete(self, ctx: TenantContext, integration_id: str) -> None:
        ctx.require(Permission.MANAGE_GROUP)
        with self.store.session(ctx) as s:
            integ = self._load(s, ctx, integration_id)
            for route in s.scalars(
                select(NotificationRoute).where(
                    NotificationRoute.group_id == ctx.group_id,
                    NotificationRoute.integration_id == integration_id,
                )
            ).all():
                s.delete(route)
            for d in s.scalars(
                select(NotificationDelivery).where(
                    NotificationDelivery.group_id == ctx.group_id,
                    NotificationDelivery.integration_id == integration_id,
                )
            ).all():
                s.delete(d)
            if integ.secret_ref:
                try:
                    self.secrets.delete(ctx, integ.secret_ref)
                except Exception:  # noqa: BLE001 - best-effort secret cleanup
                    pass
            audit.record(s, ctx, "integration.delete", entity_type="integration", entity_id=integration_id)
            s.delete(integ)

    def test(self, ctx: TenantContext, integration_id: str) -> dict:
        ctx.require(Permission.MANAGE_GROUP)
        with self.store.session(ctx) as s:
            integ = self._load(s, ctx, integration_id)
            secret = self.secrets.get(ctx, integ.secret_ref) if integ.secret_ref else None
            local = bool(self.settings.is_local) if self.settings else True
            try:
                provider = build_channel_provider(
                    integ.provider, integ.config, secret, local=local, notifier=self.notifier
                )
                res = provider.send(
                    "DV Platform test notification",
                    f"This is a test from the '{integ.name}' {integ.provider} integration.",
                )
                ok, detail = res.ok, res.detail
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"provider error: {exc}"
            s.add(NotificationDelivery(
                org_id=ctx.org_id, group_id=ctx.group_id, integration_id=integ.id, event="test",
                entity_type="test", entity_id=None, status="sent" if ok else "failed",
                detail={"detail": detail}, ts=utcnow(),
            ))
            integ.last_delivery_at = utcnow()
            integ.status = "active" if ok else "error"
            integ.last_error = None if ok else detail
            audit.record(s, ctx, "integration.test", entity_type="integration", entity_id=integ.id,
                         detail={"ok": ok, "detail": detail})
            return {"ok": ok, "detail": detail}

    def _load(self, s, ctx: TenantContext, integration_id: str) -> Integration:  # noqa: ANN001
        integ = s.scalar(
            select(Integration).where(
                Integration.id == integration_id, Integration.group_id == ctx.group_id
            )
        )
        if integ is None:
            raise NotFoundError("integration not found", details={"id": integration_id})
        return integ


class RouteService:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def list(self, ctx: TenantContext) -> list[dict]:
        ctx.require(Permission.MANAGE_GROUP)
        with self.store.session(ctx) as s:
            rows = s.scalars(
                select(NotificationRoute).where(NotificationRoute.group_id == ctx.group_id)
            ).all()
            return [_route_dict(r) for r in rows]

    def create(self, ctx: TenantContext, *, event: str, min_severity: str, integration_id: str, enabled: bool = True) -> dict:
        ctx.require(Permission.MANAGE_GROUP)
        if event not in _EVENTS:
            raise ValidationError("unknown event", details={"event": event, "allowed": list(_EVENTS)})
        if min_severity not in specs.SEVERITY_ORDER:
            raise ValidationError("unknown severity", details={"min_severity": min_severity})
        with self.store.session(ctx) as s:
            integ = s.scalar(
                select(Integration).where(
                    Integration.id == integration_id, Integration.group_id == ctx.group_id
                )
            )
            if integ is None:
                raise ValidationError("integration not found", details={"integration_id": integration_id})
            r = NotificationRoute(
                org_id=ctx.org_id, group_id=ctx.group_id, event=event, min_severity=min_severity,
                integration_id=integration_id, enabled=enabled, created_by=ctx.user_id,
            )
            s.add(r)
            s.flush()
            audit.record(s, ctx, "route.create", entity_type="notification_route", entity_id=r.id,
                         detail={"event": event, "integration_id": integration_id})
            return _route_dict(r)

    def update(self, ctx: TenantContext, route_id: str, *, min_severity=None, enabled=None) -> dict:  # noqa: ANN001
        ctx.require(Permission.MANAGE_GROUP)
        with self.store.session(ctx) as s:
            r = s.scalar(
                select(NotificationRoute).where(
                    NotificationRoute.id == route_id, NotificationRoute.group_id == ctx.group_id
                )
            )
            if r is None:
                raise NotFoundError("route not found", details={"id": route_id})
            if min_severity is not None:
                if min_severity not in specs.SEVERITY_ORDER:
                    raise ValidationError("unknown severity", details={"min_severity": min_severity})
                r.min_severity = min_severity
            if enabled is not None:
                r.enabled = bool(enabled)
            audit.record(s, ctx, "route.update", entity_type="notification_route", entity_id=r.id)
            return _route_dict(r)

    def delete(self, ctx: TenantContext, route_id: str) -> None:
        ctx.require(Permission.MANAGE_GROUP)
        with self.store.session(ctx) as s:
            r = s.scalar(
                select(NotificationRoute).where(
                    NotificationRoute.id == route_id, NotificationRoute.group_id == ctx.group_id
                )
            )
            if r is None:
                raise NotFoundError("route not found", details={"id": route_id})
            audit.record(s, ctx, "route.delete", entity_type="notification_route", entity_id=route_id)
            s.delete(r)


def _split(provider: str, values: dict) -> tuple[dict, dict]:
    """Partition raw form values into non-secret config and secret material."""
    secret_keys = specs.secret_field_keys(provider)
    config, secret = {}, {}
    for k, v in (values or {}).items():
        if v is None or v == "":
            continue
        (secret if k in secret_keys else config)[k] = v
    return config, secret


def _dict(r: Integration) -> dict:
    return {
        "id": r.id,
        "category": r.category,
        "provider": r.provider,
        "name": r.name,
        "enabled": r.enabled,
        "config": r.config or {},
        "has_secret": bool(r.secret_ref),
        "status": r.status,
        "last_delivery_at": r.last_delivery_at.isoformat() if r.last_delivery_at else None,
        "last_error": r.last_error,
    }


def _route_dict(r: NotificationRoute) -> dict:
    return {
        "id": r.id,
        "event": r.event,
        "min_severity": r.min_severity,
        "integration_id": r.integration_id,
        "enabled": r.enabled,
    }


def _delivery_dict(r: NotificationDelivery) -> dict:
    return {
        "id": r.id,
        "integration_id": r.integration_id,
        "event": r.event,
        "entity_type": r.entity_type,
        "entity_id": r.entity_id,
        "status": r.status,
        "detail": r.detail or {},
        "ts": r.ts.isoformat() if r.ts else None,
    }
