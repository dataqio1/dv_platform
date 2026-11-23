"""CatalogCacheStore — read/write/clear cached catalog listings (schemas, tables, databases).

Listing a warehouse's catalog can be slow, so we cache each listing per connection under a
stable key and serve it instantly until an explicit refresh. Shared across the tenant and
persisted, so it survives restarts and benefits every user + the Find Column table scan.
"""

from __future__ import annotations

from datetime import UTC, datetime

from dvp.core.tenancy import TenantContext
from dvp.db.base import utcnow
from dvp.db.models import CatalogCache
from dvp.db.session import MetadataStore


def _iso(dt: datetime | None) -> str | None:
    """Stable UTC-aware ISO string. SQLite reads timestamps back naive, so normalize to UTC
    for a representation that matches freshly-written (tz-aware) values."""
    if dt is None:
        return None
    return (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt).isoformat()


def schemas_key(database: str | None) -> str:
    return f"schemas:{database or ''}"


def tables_key(database: str | None, schema: str) -> str:
    return f"tables:{database or ''}:{schema}"


def databases_key() -> str:
    return "databases"


class CatalogCacheStore:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def get(self, ctx: TenantContext, connection_id: str, key: str) -> tuple[dict, str] | None:
        """Return (payload, fetched_at_iso) or None on miss."""
        from sqlalchemy import select

        with self.store.session(ctx) as s:
            row = s.scalars(
                select(CatalogCache).where(
                    CatalogCache.group_id == ctx.group_id,
                    CatalogCache.connection_id == connection_id,
                    CatalogCache.cache_key == key,
                )
            ).first()
            if row is None:
                return None
            return dict(row.payload or {}), _iso(row.fetched_at)

    def put(self, ctx: TenantContext, connection_id: str, key: str, payload: dict) -> str:
        """Upsert a cached payload; returns the fetched_at ISO timestamp."""
        from sqlalchemy import select

        now = utcnow()
        with self.store.session(ctx) as s:
            row = s.scalars(
                select(CatalogCache).where(
                    CatalogCache.group_id == ctx.group_id,
                    CatalogCache.connection_id == connection_id,
                    CatalogCache.cache_key == key,
                )
            ).first()
            if row is None:
                row = CatalogCache(
                    org_id=ctx.org_id, group_id=ctx.group_id, connection_id=connection_id,
                    cache_key=key, payload=payload, fetched_at=now,
                )
                s.add(row)
            else:
                row.payload = payload
                row.fetched_at = now
            s.flush()
        return _iso(now)

    def clear(self, ctx: TenantContext, connection_id: str, key: str | None = None) -> int:
        """Drop one cache entry, or every catalog entry for the connection (key=None)."""
        from sqlalchemy import delete

        with self.store.session(ctx) as s:
            stmt = delete(CatalogCache).where(
                CatalogCache.group_id == ctx.group_id,
                CatalogCache.connection_id == connection_id,
            )
            if key is not None:
                stmt = stmt.where(CatalogCache.cache_key == key)
            result = s.execute(stmt)
            return int(result.rowcount or 0)
