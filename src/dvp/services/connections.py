"""ConnectionService — CRUD for data-source connections.

Coordinates live in the metadata DB; secret material (private keys, OAuth refresh
tokens, passwords) is written to the SecretStore and referenced by `secret_ref`.
"""

from __future__ import annotations

from sqlalchemy import select

from dvp.abstractions.secret_store import SecretStore, secret_path
from dvp.core.errors import NotFoundError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.models import Connection
from dvp.db.session import MetadataStore
from dvp.services import audit


class ConnectionService:
    def __init__(self, store: MetadataStore, secrets: SecretStore) -> None:
        self.store = store
        self.secrets = secrets

    def create(
        self,
        ctx: TenantContext,
        *,
        source_type: str,
        name: str,
        coordinates: dict,
        auth_method: str,
        secret: dict | None = None,
        allowed_scopes: dict | None = None,
        metadata_columns: dict | None = None,
        metadata_convention_ids: list[str] | None = None,
    ) -> dict:
        ctx.require(Permission.MANAGE_CONNECTIONS)
        with self.store.session(ctx) as s:
            conn = Connection(
                org_id=ctx.org_id,
                group_id=ctx.group_id,
                source_type=source_type,
                name=name,
                coordinates=coordinates,
                auth_method=auth_method,
                allowed_scopes=allowed_scopes or {},
                metadata_columns=metadata_columns or None,
                metadata_convention_ids=metadata_convention_ids or None,
                created_by=ctx.user_id,
            )
            s.add(conn)
            s.flush()
            if secret:
                path = secret_path(ctx, conn.id)
                self.secrets.put(ctx, path, secret)
                conn.secret_ref = path
            audit.record(s, ctx, "connection.create", entity_type="connection", entity_id=conn.id)
            return _to_dict(conn)

    def list(self, ctx: TenantContext) -> list[dict]:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            rows = s.scalars(select(Connection).where(Connection.group_id == ctx.group_id)).all()
            return [_to_dict(c) for c in rows]

    def get(self, ctx: TenantContext, connection_id: str) -> dict:
        ctx.require(Permission.READ)
        return _to_dict(self._load(ctx, connection_id))

    def test(self, ctx: TenantContext, connection_id: str) -> dict:
        """Actually connect to the source and report a typed health result."""
        ctx.require(Permission.READ)
        from dvp.connectors.registry import build_connector

        with self.store.session(ctx) as s:
            conn = self._load_in_session(s, ctx, connection_id)
            source_type = conn.source_type
            coordinates = dict(conn.coordinates)
            auth_method = conn.auth_method
            secret_ref = conn.secret_ref
        secret = self.secrets.get(ctx, secret_ref) if secret_ref else None
        connector = build_connector(source_type, coordinates, auth_method, secret)
        result = connector.test()
        return {"ok": result.ok, "detail": result.detail, "category": result.category}

    def delete(self, ctx: TenantContext, connection_id: str) -> None:
        ctx.require(Permission.MANAGE_CONNECTIONS)
        with self.store.session(ctx) as s:
            conn = self._load_in_session(s, ctx, connection_id)
            if conn.secret_ref:
                self.secrets.delete(ctx, conn.secret_ref)
            s.delete(conn)
            audit.record(
                s, ctx, "connection.delete", entity_type="connection", entity_id=connection_id
            )

    # ----- helpers -----
    def _load(self, ctx: TenantContext, connection_id: str) -> Connection:
        with self.store.session(ctx) as s:
            return self._load_in_session(s, ctx, connection_id)

    @staticmethod
    def _load_in_session(s, ctx: TenantContext, connection_id: str) -> Connection:  # noqa: ANN001
        conn = s.get(Connection, connection_id)
        if conn is None or conn.group_id != ctx.group_id:
            raise NotFoundError("connection not found", details={"id": connection_id})
        return conn


def _to_dict(c: Connection) -> dict:
    return {
        "id": c.id,
        "source_type": c.source_type,
        "name": c.name,
        "coordinates": c.coordinates,
        "auth_method": c.auth_method,
        "has_secret": c.secret_ref is not None,
        "allowed_scopes": c.allowed_scopes,
        "status": c.status,
        "metadata_columns": c.metadata_columns or None,
        "metadata_convention_ids": c.metadata_convention_ids or [],
    }
