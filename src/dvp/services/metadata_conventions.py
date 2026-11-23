"""MetadataConventionService — the named ETL metadata-column convention library (docs/design/11 §3.4).

A convention is a reusable role→column map ("IDMC-CDC", "S3-Loader"). Shipped presets
(`services.table_metadata.METADATA_PRESETS`) are served as read-only **system** conventions with
`sys:<key>` ids; rows in `metadata_convention` are user-defined. Connections attach one or more by
id; a connection loaded by two ETL tools attaches both and each table resolves to the best-covering
one (coverage-based, see `table_metadata.resolve_conventions`).
"""

from __future__ import annotations

from sqlalchemy import select

from dvp.core.errors import NotFoundError, ValidationError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.models import MetadataConvention
from dvp.db.session import MetadataStore
from dvp.services import audit
from dvp.services.table_metadata import METADATA_PRESETS

_SYS = "sys:"


def _system_conventions() -> list[dict]:
    """Shipped presets exposed as read-only conventions (stable `sys:<key>` ids)."""
    out = []
    for key, p in METADATA_PRESETS.items():
        if not p.get("roles"):
            continue  # skip 'custom' (empty) — nothing to attach
        out.append({
            "id": f"{_SYS}{key}", "name": p.get("label", key), "roles": dict(p["roles"]),
            "scd2": p.get("scd2"), "system": True,
        })
    return out


class MetadataConventionService:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def list(self, ctx: TenantContext) -> list[dict]:
        """Every convention available to attach: shipped `sys:` presets + this group's own."""
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            rows = s.scalars(
                select(MetadataConvention)
                .where(MetadataConvention.group_id == ctx.group_id)
                .order_by(MetadataConvention.name)
            ).all()
            user = [_dict(r) for r in rows]
        return _system_conventions() + user

    def get(self, ctx: TenantContext, conv_id: str) -> dict:
        """Resolve one convention by id (`sys:<key>` preset or a stored row)."""
        if conv_id.startswith(_SYS):
            found = next((c for c in _system_conventions() if c["id"] == conv_id), None)
            if found is None:
                raise NotFoundError("unknown system convention", details={"id": conv_id})
            return found
        with self.store.session(ctx) as s:
            return _dict(self._load(s, ctx, conv_id))

    def resolve_ids(self, ctx: TenantContext, ids: list[str] | None) -> list[dict]:
        """Resolve a list of attached ids to convention dicts (order preserved), skipping any that
        no longer exist — attachments must never hard-fail detection."""
        if not ids:
            return []
        sys_by_id = {c["id"]: c for c in _system_conventions()}
        out: list[dict] = []
        with self.store.session(ctx) as s:
            rows = {
                r.id: _dict(r)
                for r in s.scalars(
                    select(MetadataConvention).where(
                        MetadataConvention.group_id == ctx.group_id,
                        MetadataConvention.id.in_([i for i in ids if not i.startswith(_SYS)]),
                    )
                ).all()
            }
        for i in ids:
            c = sys_by_id.get(i) if i.startswith(_SYS) else rows.get(i)
            if c is not None:
                out.append(c)
        return out

    def create(self, ctx: TenantContext, *, name: str, roles: dict, scd2: dict | None = None) -> dict:
        ctx.require(Permission.WRITE)
        if not (name or "").strip():
            raise ValidationError("a convention needs a name")
        if not roles:
            raise ValidationError("a convention needs at least one role→column mapping")
        with self.store.session(ctx) as s:
            row = MetadataConvention(
                org_id=ctx.org_id, group_id=ctx.group_id, name=name.strip(),
                roles=dict(roles), scd2=scd2 or None, created_by=ctx.user_id,
            )
            s.add(row)
            s.flush()
            audit.record(s, ctx, "metadata_convention.create",
                         entity_type="metadata_convention", entity_id=row.id, detail={"name": name})
            return _dict(row)

    def update(self, ctx: TenantContext, conv_id: str, patch: dict) -> dict:
        ctx.require(Permission.WRITE)
        if conv_id.startswith(_SYS):
            raise ValidationError("shipped conventions are read-only; save a copy instead")
        with self.store.session(ctx) as s:
            row = self._load(s, ctx, conv_id)
            for f in ("name", "roles", "scd2"):
                if f in patch and patch[f] is not None:
                    setattr(row, f, patch[f])
            audit.record(s, ctx, "metadata_convention.update",
                         entity_type="metadata_convention", entity_id=conv_id)
            return _dict(row)

    def delete(self, ctx: TenantContext, conv_id: str) -> None:
        ctx.require(Permission.WRITE)
        if conv_id.startswith(_SYS):
            raise ValidationError("shipped conventions cannot be deleted")
        with self.store.session(ctx) as s:
            s.delete(self._load(s, ctx, conv_id))
            audit.record(s, ctx, "metadata_convention.delete",
                         entity_type="metadata_convention", entity_id=conv_id)

    def _load(self, s, ctx: TenantContext, conv_id: str) -> MetadataConvention:  # noqa: ANN001
        row = s.scalar(
            select(MetadataConvention).where(
                MetadataConvention.id == conv_id, MetadataConvention.group_id == ctx.group_id
            )
        )
        if row is None:
            raise NotFoundError("convention not found", details={"id": conv_id})
        return row


def _dict(r: MetadataConvention) -> dict:
    return {"id": r.id, "name": r.name, "roles": r.roles or {}, "scd2": r.scd2 or None,
            "system": False}
