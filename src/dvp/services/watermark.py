"""WatermarkService — detect & validate a monitor's watermark column against the
real source schema.

Flow mirrors ConnectionService.test: load the connection, build its connector, fetch
each selected table's schema, then run the pure detection/validation logic. Per-table
schema-fetch failures are collected as warnings (never swallowed — golden rule #3) so a
single unreadable table doesn't sink the whole request.
"""

from __future__ import annotations

from dvp.abstractions.secret_store import SecretStore
from dvp.connectors.base import TableRef
from dvp.core.errors import ValidationError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.models import Connection
from dvp.db.session import MetadataStore
from dvp.observability.watermark import ColumnInfo, detect_watermark, validate_watermark


class WatermarkService:
    def __init__(self, store: MetadataStore, secrets: SecretStore) -> None:
        self.store = store
        self.secrets = secrets

    def detect(self, ctx: TenantContext, connection_id: str, scope: dict) -> dict:
        ctx.require(Permission.READ)
        schemas, warnings = self._load_schemas(ctx, connection_id, scope)
        suggestions = detect_watermark(schemas)
        return {
            "connection_id": connection_id,
            "tables_inspected": list(schemas.keys()),
            "suggested": suggestions[0].as_dict() if suggestions else None,
            "candidates": [s.as_dict() for s in suggestions],
            "warnings": warnings,
        }

    def validate(
        self,
        ctx: TenantContext,
        connection_id: str,
        scope: dict,
        column: str,
        expected_type: str | None = None,
    ) -> dict:
        ctx.require(Permission.READ)
        if not column:
            raise ValidationError("watermark column is required")
        schemas, warnings = self._load_schemas(ctx, connection_id, scope)
        result = validate_watermark(schemas, column, expected_type)
        result["connection_id"] = connection_id
        result["warnings"] = warnings
        return result

    # ----- helpers -----
    def _load_schemas(
        self, ctx: TenantContext, connection_id: str, scope: dict
    ) -> tuple[dict[str, list[ColumnInfo]], list[dict]]:
        from dvp.connectors.registry import build_connector

        with self.store.session(ctx) as s:
            conn = s.get(Connection, connection_id)
            if conn is None or conn.group_id != ctx.group_id:
                raise ValidationError(
                    "connection not found", details={"connection_id": connection_id}
                )
            source_type = conn.source_type
            coordinates = dict(conn.coordinates)
            auth_method = conn.auth_method
            secret_ref = conn.secret_ref

        secret = self.secrets.get(ctx, secret_ref) if secret_ref else None
        connector = build_connector(source_type, coordinates, auth_method, secret)

        database = scope.get("database") or coordinates.get("database") or ""
        schema_name = scope.get("schema") or coordinates.get("schema") or ""
        tables = scope.get("tables") or []
        if not tables and hasattr(connector, "list_tables"):
            tables = connector.list_tables()
        if not tables:
            raise ValidationError(
                "scope must list at least one table (scope.tables) to inspect",
                details={"scope": scope},
            )

        schemas: dict[str, list[ColumnInfo]] = {}
        warnings: list[dict] = []
        for table in tables:
            ref = TableRef(database=database, schema=schema_name, table=table)
            try:
                cols = connector.get_schema(ref)
                schemas[table] = [
                    ColumnInfo(name=c.name, data_type=c.data_type, nullable=c.nullable)
                    for c in cols
                ]
            except Exception as exc:  # noqa: BLE001 - surface, don't swallow
                warnings.append(
                    {
                        "code": "schema_fetch_failed",
                        "message": f"could not read schema for table '{table}': {exc}",
                        "details": {"table": table},
                    }
                )
        return schemas, warnings
