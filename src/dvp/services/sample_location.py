"""Group setting for where compare exception rows are stored.

Null keeps samples on the platform result store. A location writes them into a
table on a connection the group owns — Snowflake in production — and the platform
keeps the counts plus a pointer.
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from dvp.abstractions.result_store import ResultStore, WarehouseResultStore, parse_warehouse_ref
from dvp.core.errors import PermissionDeniedError, ValidationError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.models import Connection, Group
from dvp.db.session import MetadataStore
from dvp.services import audit

_IDENT = re.compile(r"^[A-Z_][A-Z0-9_]{0,254}$")
_WAREHOUSE_TYPES = {"snowflake", "local", "local_sql", "sqlite"}


def _ident(value: str, label: str) -> str:
    name = (value or "").strip().upper()
    if not _IDENT.match(name):
        raise ValidationError(f"{label} must be letters, numbers, and underscores")
    return name


def _load_group(s: Session, ctx: TenantContext) -> Group:
    group = s.get(Group, ctx.group_id)
    if group is None or group.org_id != ctx.org_id:
        raise PermissionDeniedError("group not found")
    return group


def _public(raw: dict | None, connection_name: str | None = None) -> dict:
    if not raw:
        return {
            "backend": "local",
            "connection_id": None,
            "connection_name": None,
            "database": None,
            "schema": None,
            "table": None,
            "location": None,
        }
    database, schema, table = raw["database"], raw["schema"], raw["table"]
    return {
        "backend": "snowflake",
        "connection_id": raw["connection_id"],
        "connection_name": connection_name,
        "database": database,
        "schema": schema,
        "table": table,
        "location": f"{database}.{schema}.{table}",
    }


class SampleLocationService:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def get(self, ctx: TenantContext) -> dict:
        ctx.require(Permission.MANAGE_GROUP)
        with self.store.session(ctx) as s:
            group = _load_group(s, ctx)
            raw = dict(group.sample_store) if group.sample_store else None
            name = None
            if raw:
                conn = s.get(Connection, raw.get("connection_id"))
                if conn is not None and conn.group_id == ctx.group_id:
                    name = conn.name
        return _public(raw, name)

    def set(
        self,
        ctx: TenantContext,
        *,
        connection_id: str,
        database: str,
        schema: str,
        table: str,
    ) -> dict:
        ctx.require(Permission.MANAGE_GROUP)
        database = _ident(database, "database")
        schema = _ident(schema, "schema")
        table = _ident(table or "DIFF_SAMPLES", "table")
        with self.store.session(ctx) as s:
            conn = s.get(Connection, connection_id)
            if conn is None or conn.group_id != ctx.group_id:
                raise ValidationError("connection not found", details={"id": connection_id})
            if conn.source_type not in _WAREHOUSE_TYPES:
                raise ValidationError(
                    "Exception rows stay in Snowflake. Pick a Snowflake connection."
                )
            group = _load_group(s, ctx)
            before = _public(dict(group.sample_store) if group.sample_store else None, None)
            raw = {
                "connection_id": connection_id,
                "database": database,
                "schema": schema,
                "table": table,
            }
            group.sample_store = raw
            after = _public(raw, conn.name)
            audit.record(
                s,
                ctx,
                "sample_store.update",
                entity_type="group",
                entity_id=ctx.group_id,
                detail={"before": before, "after": after},
            )
        return after

    def clear(self, ctx: TenantContext) -> dict:
        ctx.require(Permission.MANAGE_GROUP)
        with self.store.session(ctx) as s:
            group = _load_group(s, ctx)
            conn_name = None
            if group.sample_store:
                conn = s.get(Connection, group.sample_store.get("connection_id"))
                if conn is not None and conn.group_id == ctx.group_id:
                    conn_name = conn.name
            before = _public(dict(group.sample_store) if group.sample_store else None, conn_name)
            group.sample_store = None
            after = _public(None)
            audit.record(
                s,
                ctx,
                "sample_store.update",
                entity_type="group",
                entity_id=ctx.group_id,
                detail={"before": before, "after": after},
            )
        return after


def _open_connector(store: MetadataStore, secrets, ctx: TenantContext, connection_id: str):  # noqa: ANN001
    from dvp.connectors.registry import build_connector

    with store.session(ctx) as s:
        conn = s.get(Connection, connection_id)
        if conn is None or conn.group_id != ctx.group_id:
            raise PermissionDeniedError("sample pointer is outside this group")
        source_type, coords = conn.source_type, dict(conn.coordinates or {})
        auth, secret_ref = conn.auth_method, conn.secret_ref
    secret = secrets.get(ctx, secret_ref) if (secret_ref and secrets) else None
    return build_connector(source_type, coords, auth, secret)


def _warehouse(
    store: MetadataStore, secrets, ctx: TenantContext, raw: dict
) -> WarehouseResultStore:  # noqa: ANN001
    connector = _open_connector(store, secrets, ctx, raw["connection_id"])
    return WarehouseResultStore(
        connector,
        connection_id=raw["connection_id"],
        database=raw["database"],
        schema=raw["schema"],
        table=raw["table"],
    )


def configured_writer(store: MetadataStore, secrets, ctx: TenantContext) -> ResultStore | None:  # noqa: ANN001
    """The group's warehouse store, or None when samples stay on the platform."""
    with store.session(ctx) as s:
        group = s.get(Group, ctx.group_id)
        raw = dict(group.sample_store) if group and group.sample_store else None
    if not raw:
        return None
    return _warehouse(store, secrets, ctx, raw)


def store_for_ref(
    store: MetadataStore,
    secrets,  # noqa: ANN001
    ctx: TenantContext,
    ref: str | None,
    fallback: ResultStore,
) -> ResultStore:
    """Read or delete through the store that owns this pointer."""
    parsed = parse_warehouse_ref(ref or "")
    if parsed is None:
        return fallback
    if not parsed.sample_key.startswith(f"{ctx.group_id}/"):
        raise PermissionDeniedError("sample pointer is outside this group")
    return _warehouse(
        store,
        secrets,
        ctx,
        {
            "connection_id": parsed.connection_id,
            "database": parsed.database,
            "schema": parsed.schema,
            "table": parsed.table,
        },
    )
