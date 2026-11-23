"""CatalogService — browse a connection's schemas and tables/views.

Schema/table listing is database-specific (Snowflake INFORMATION_SCHEMA, Oracle
ALL_USERS/ALL_TABLES/ALL_VIEWS, files/local = the loaded tables). Each connector owns
its own listing; this service builds the connector and delegates, with graceful
fallbacks so the UI can always offer *something* (and fall back to manual entry) rather
than erroring — introspection failures come back as warnings, never exceptions.
"""

from __future__ import annotations

import inspect

from dvp.abstractions.secret_store import SecretStore
from dvp.core.errors import ValidationError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.models import Connection
from dvp.db.session import MetadataStore
from dvp.services.catalog_cache import (
    CatalogCacheStore,
    databases_key,
    schemas_key,
    tables_key,
)


def _accepts(fn, param: str) -> bool:
    """Whether a connector method accepts a given keyword — so multi-database sources
    (Snowflake) can be browsed by database while single-database ones aren't broken."""
    try:
        return param in inspect.signature(fn).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins without a signature
        return False


class CatalogService:
    def __init__(self, store: MetadataStore, secrets: SecretStore) -> None:
        self.store = store
        self.secrets = secrets
        self.cache = CatalogCacheStore(store)

    def refresh_cache(self, ctx: TenantContext, connection_id: str) -> dict:
        """Drop every cached catalog listing for a connection and re-fetch its schemas.
        The schema/table pickers (and the Find Column table scan) then repopulate the cache."""
        ctx.require(Permission.READ)
        cleared = self.cache.clear(ctx, connection_id)
        fresh = self.list_schemas(ctx, connection_id, refresh=True)
        return {**fresh, "cleared": cleared}

    def list_databases(
        self, ctx: TenantContext, connection_id: str, refresh: bool = False
    ) -> dict:
        """Databases the connection can read. Sources that aren't multi-database (Oracle,
        files, inline) fall back to the connection's single coordinates.database."""
        ctx.require(Permission.READ)
        key = databases_key()
        if not refresh:
            hit = self.cache.get(ctx, connection_id, key)
            if hit is not None:
                return {"databases": hit[0].get("databases", []), "warnings": [],
                        "cached": True, "cached_at": hit[1]}
        connector, coords = self._connector(ctx, connection_id)
        default = coords.get("database")
        fallback = [default] if default else []
        if hasattr(connector, "list_databases"):
            try:
                dbs = connector.list_databases() or fallback
                return self._cached(ctx, connection_id, key, {"databases": dbs})
            except Exception as exc:  # noqa: BLE001 - surface as warning, don't 500
                return {"databases": fallback, "warnings": [_warn("database_list_failed", str(exc))]}
        return self._cached(ctx, connection_id, key, {"databases": fallback})

    def list_schemas(
        self, ctx: TenantContext, connection_id: str, database: str | None = None,
        refresh: bool = False,
    ) -> dict:
        ctx.require(Permission.READ)
        key = schemas_key(database)
        if not refresh:
            hit = self.cache.get(ctx, connection_id, key)
            if hit is not None:
                return {"schemas": hit[0].get("schemas", []), "warnings": [],
                        "cached": True, "cached_at": hit[1]}
        connector, coords = self._connector(ctx, connection_id)
        default = coords.get("schema") or "main"
        if hasattr(connector, "list_schemas"):
            try:
                if database and _accepts(connector.list_schemas, "database"):
                    schemas = connector.list_schemas(database=database)
                else:
                    schemas = connector.list_schemas()
                # When a specific database was requested don't mask an empty result with the
                # connection-default schema — empty means "no schemas visible there".
                schemas = schemas if database else (schemas or [default])
                return self._cached(ctx, connection_id, key, {"schemas": schemas})
            except Exception as exc:  # noqa: BLE001 - surface as warning, don't 500
                return {
                    "schemas": [] if database else ([default] if coords.get("schema") else []),
                    "warnings": [_warn("schema_list_failed", str(exc))],
                }
        return self._cached(ctx, connection_id, key, {"schemas": [default]})

    def list_tables(
        self, ctx: TenantContext, connection_id: str, schema: str, database: str | None = None,
        refresh: bool = False,
    ) -> dict:
        ctx.require(Permission.READ)
        key = tables_key(database, schema)
        if not refresh:
            hit = self.cache.get(ctx, connection_id, key)
            if hit is not None:
                return {"tables": hit[0].get("tables", []), "warnings": [],
                        "cached": True, "cached_at": hit[1]}
        connector, _ = self._connector(ctx, connection_id)
        if hasattr(connector, "list_tables_in_schema"):
            try:
                if database and _accepts(connector.list_tables_in_schema, "database"):
                    tables = connector.list_tables_in_schema(schema, database=database)
                else:
                    tables = connector.list_tables_in_schema(schema)
                return self._cached(ctx, connection_id, key, {"tables": tables})
            except Exception as exc:  # noqa: BLE001
                return {"tables": [], "warnings": [_warn("table_list_failed", str(exc))]}
        if hasattr(connector, "list_tables"):
            try:
                tables = [{"name": n, "type": "table"} for n in connector.list_tables()]
                return self._cached(ctx, connection_id, key, {"tables": tables})
            except Exception as exc:  # noqa: BLE001
                return {"tables": [], "warnings": [_warn("table_list_failed", str(exc))]}
        return {"tables": [], "warnings": [_warn("not_supported", "source can't list tables")]}

    def _cached(self, ctx: TenantContext, connection_id: str, key: str, payload: dict) -> dict:
        """Persist a freshly-fetched listing and return it tagged with its fetch time."""
        fetched_at = self.cache.put(ctx, connection_id, key, payload)
        return {**payload, "warnings": [], "cached": False, "cached_at": fetched_at}

    def list_columns(
        self, ctx: TenantContext, connection_id: str, schema: str, table: str, database: str | None = None
    ) -> dict:
        """Columns for one table/view (name, data_type, nullable) — feeds the per-table
        load-pattern pickers (watermark/partition/scd2 columns) and the auto-classifier."""
        ctx.require(Permission.READ)
        from dvp.connectors.base import TableRef

        connector, coords = self._connector(ctx, connection_id)
        db = database or coords.get("database") or ""
        ref = TableRef(database=db, schema=schema, table=table)
        try:
            cols = connector.get_schema(ref)
            return {
                "columns": [
                    {"name": c.name, "data_type": c.data_type, "nullable": c.nullable} for c in cols
                ],
                "warnings": [],
            }
        except Exception as exc:  # noqa: BLE001 - surface as warning, don't 500
            return {"columns": [], "warnings": [_warn("column_list_failed", str(exc))]}

    def row_count(
        self, ctx: TenantContext, connection_id: str, schema: str, table: str, database: str | None = None
    ) -> dict:
        """Approximate row count for a table. Prefers a cheap warehouse metadata estimate
        (e.g. Snowflake INFORMATION_SCHEMA.ROW_COUNT — instant, no scan) so the picker can
        show ~counts for huge tables without a COUNT(*); falls back to an exact count."""
        ctx.require(Permission.READ)
        from dvp.connectors.base import TableRef

        connector, coords = self._connector(ctx, connection_id)
        ref = TableRef(database=database or coords.get("database") or "", schema=schema, table=table)
        if hasattr(connector, "metadata_row_count"):
            try:
                est = connector.metadata_row_count(ref)
                if est is not None:
                    return {"count": int(est), "approx": True, "warnings": []}
            except Exception:  # noqa: BLE001 - fall through to exact count
                pass
        try:
            return {"count": int(connector.count(ref)), "approx": False, "warnings": []}
        except Exception as exc:  # noqa: BLE001 - surface as warning, don't 500
            return {"count": None, "approx": False, "warnings": [_warn("row_count_failed", str(exc))]}

    def sample(
        self, ctx: TenantContext, connection_id: str, schema: str, table: str,
        database: str | None = None, limit: int = 20,
    ) -> dict:
        """A few rows from a table/view for preview (capped). Dialect-aware LIMIT so it's
        cheap on any source; values are JSON-safe (datetimes/decimals/bytes → strings)."""
        ctx.require(Permission.READ)
        from dvp.connectors.base import TableRef

        connector, coords = self._connector(ctx, connection_id)
        ref = TableRef(database=database or coords.get("database") or "", schema=schema, table=table)
        n = max(1, min(int(limit or 20), 100))
        try:
            # Prefer a native sampler (inline/file sources can't run arbitrary SQL); else a
            # dialect-aware SELECT * … LIMIT via the connector's SQL engine.
            if hasattr(connector, "sample_rows"):
                cols, raw = connector.sample_rows(ref, n)
            else:
                ref_sql = connector.sql_ref(ref) if hasattr(connector, "sql_ref") else ref.fqn()
                res = connector.execute(_sample_sql(getattr(connector, "dialect", ""), ref_sql, n))
                cols, raw = res.columns, res.rows
            rows = [[_json_safe(v) for v in r] for r in list(raw)[:n]]
            return {"columns": list(cols), "rows": rows, "warnings": []}
        except Exception as exc:  # noqa: BLE001 - surface as warning, don't 500
            return {"columns": [], "rows": [], "warnings": [_warn("sample_failed", str(exc))]}

    def preview_sql(
        self, ctx: TenantContext, connection_id: str, sql: str,
        database: str | None = None, limit: int = 20,
    ) -> dict:
        """Validate an arbitrary SQL dataset by actually running it (capped to `limit` rows)
        against the connection. Returns the column list + a sample so the user can confirm the
        query is valid and returns what they expect BEFORE building a test case on it. A syntax
        or permission error comes back as a warning (never a 500) so the UI can show it inline."""
        ctx.require(Permission.READ)
        connector, _ = self._connector(ctx, connection_id)
        n = max(1, min(int(limit or 20), 100))
        inner = (sql or "").strip().rstrip(";").strip()
        if not inner:
            return {"ok": False, "columns": [], "rows": [], "row_count": 0,
                    "warnings": [_warn("empty_sql", "Enter a SQL query to validate.")]}
        if not hasattr(connector, "execute"):
            return {"ok": False, "columns": [], "rows": [], "row_count": 0,
                    "warnings": [_warn("not_supported", "this source cannot run SQL queries")]}
        try:
            res = connector.execute(_preview_sql(getattr(connector, "dialect", ""), inner, n))
            rows = [[_json_safe(v) for v in r] for r in list(res.rows)[:n]]
            return {"ok": True, "columns": list(res.columns), "rows": rows,
                    "row_count": len(rows), "warnings": []}
        except Exception as exc:  # noqa: BLE001 - surface the DB error inline, don't 500
            return {"ok": False, "columns": [], "rows": [], "row_count": 0,
                    "warnings": [_warn("sql_invalid", str(exc))]}

    def _connector(self, ctx: TenantContext, connection_id: str):
        from dvp.connectors.registry import build_connector

        with self.store.session(ctx) as s:
            conn = s.get(Connection, connection_id)
            if conn is None or conn.group_id != ctx.group_id:
                raise ValidationError("connection not found", details={"id": connection_id})
            source_type, coords = conn.source_type, dict(conn.coordinates)
            auth, secret_ref = conn.auth_method, conn.secret_ref
        secret = self.secrets.get(ctx, secret_ref) if (secret_ref and self.secrets) else None
        return build_connector(source_type, coords, auth, secret), coords


def _sample_sql(dialect: str, ref_sql: str, limit: int) -> str:
    """A `SELECT * … LIMIT n` in the source's dialect (Oracle uses FETCH FIRST)."""
    if dialect == "oracle":
        return f"SELECT * FROM {ref_sql} FETCH FIRST {limit} ROWS ONLY"
    return f"SELECT * FROM {ref_sql} LIMIT {limit}"


def _preview_sql(dialect: str, inner: str, limit: int) -> str:
    """Wrap the user's query as a subquery and cap it so validation never scans the whole table.
    Oracle uses ROWNUM; everything else uses a LIMIT on an aliased subquery."""
    if dialect == "oracle":
        return f"SELECT * FROM ({inner}) WHERE ROWNUM <= {limit}"
    return f"SELECT * FROM ({inner}) AS _dvp_preview LIMIT {limit}"


def _json_safe(v):  # noqa: ANN001, ANN201
    """Coerce a cell to something JSON-serialisable (datetimes, Decimals, bytes → str)."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return repr(v)
    return str(v)


def _warn(code: str, message: str) -> dict:
    return {"code": code, "message": message, "details": None}
