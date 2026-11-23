"""TagService — first-class, managed tags. Create/rename/recolor/delete a canonical label
set; entities (projects, compare jobs, monitors) reference tags by name. Rename and delete
propagate to every entity so the label set stays consistent."""

from __future__ import annotations

from sqlalchemy import select

from dvp.core.errors import NotFoundError, ValidationError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.models import CompareJob, Monitor, Project, Tag
from dvp.db.session import MetadataStore
from dvp.services import audit

# Every table that stores tag names in a JSON `tags` list — kept in sync on rename/delete.
_TAGGABLES = (Project, CompareJob, Monitor)


class TagService:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def create(
        self,
        ctx: TenantContext,
        *,
        name: str,
        color: str | None = None,
        description: str | None = None,
    ) -> dict:
        ctx.require(Permission.WRITE)
        name = name.strip()
        if not name:
            raise ValidationError("tag name is required")
        with self.store.session(ctx) as s:
            if self._by_name(s, ctx, name):
                raise ValidationError("tag already exists", details={"name": name})
            t = Tag(
                org_id=ctx.org_id,
                group_id=ctx.group_id,
                name=name,
                color=color,
                description=description,
                created_by=ctx.user_id,
            )
            s.add(t)
            s.flush()
            audit.record(s, ctx, "tag.create", entity_type="tag", entity_id=t.id)
            return _dict(t)

    def list(self, ctx: TenantContext) -> list[dict]:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            tags = s.scalars(
                select(Tag).where(Tag.group_id == ctx.group_id).order_by(Tag.name)
            ).all()
            usage = self._usage_counts(s, ctx.group_id)
            return [{**_dict(t), "usage": usage.get(t.name, 0)} for t in tags]

    def update(
        self,
        ctx: TenantContext,
        tag_id: str,
        *,
        name: str | None = None,
        color: str | None = None,
        description: str | None = None,
    ) -> dict:
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            t = self._load(s, ctx, tag_id)
            old = t.name
            if name is not None and name.strip() and name.strip() != old:
                new = name.strip()
                if self._by_name(s, ctx, new):
                    raise ValidationError("tag already exists", details={"name": new})
                t.name = new
                self._rename_everywhere(s, ctx.group_id, old, new)
            if color is not None:
                t.color = color
            if description is not None:
                t.description = description
            audit.record(s, ctx, "tag.update", entity_type="tag", entity_id=t.id)
            return _dict(t)

    def delete(self, ctx: TenantContext, tag_id: str) -> None:
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            t = self._load(s, ctx, tag_id)
            self._remove_everywhere(s, ctx.group_id, t.name)
            s.delete(t)
            audit.record(s, ctx, "tag.delete", entity_type="tag", entity_id=tag_id)

    # ----------------------------- propagation helpers -----------------------------
    def _rename_everywhere(self, s, gid: str, old: str, new: str) -> None:  # noqa: ANN001
        for model in _TAGGABLES:
            for row in s.scalars(select(model).where(model.group_id == gid)).all():
                if old in (row.tags or []):
                    row.tags = [new if x == old else x for x in row.tags]

    def _remove_everywhere(self, s, gid: str, name: str) -> None:  # noqa: ANN001
        for model in _TAGGABLES:
            for row in s.scalars(select(model).where(model.group_id == gid)).all():
                if name in (row.tags or []):
                    row.tags = [x for x in row.tags if x != name]

    def _usage_counts(self, s, gid: str) -> dict[str, int]:  # noqa: ANN001
        counts: dict[str, int] = {}
        for model in _TAGGABLES:
            for row in s.scalars(select(model).where(model.group_id == gid)).all():
                for name in row.tags or []:
                    counts[name] = counts.get(name, 0) + 1
        return counts

    def _by_name(self, s, ctx: TenantContext, name: str) -> Tag | None:  # noqa: ANN001
        return s.scalar(select(Tag).where(Tag.group_id == ctx.group_id).where(Tag.name == name))

    def _load(self, s, ctx: TenantContext, tag_id: str) -> Tag:  # noqa: ANN001
        t = s.get(Tag, tag_id)
        if t is None or t.group_id != ctx.group_id:
            raise NotFoundError("tag not found", details={"id": tag_id})
        return t


def _dict(t: Tag) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "color": t.color,
        "description": t.description,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }
