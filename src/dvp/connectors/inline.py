"""InlineConnector — a real, dependency-free connector whose data lives in the
connection's `coordinates`.

It is the local/demo counterpart to the Snowflake connector: schema, row counts and
watermark high-values are read straight from the stored coordinates, so watermark
detection, validation and table profiling run end-to-end with zero external services
(same philosophy as the in-memory SQLite metadata store and local[*] Spark).

Coordinates shape::

    {
      "database": "RAW_SALES",
      "schema": "PUBLIC",
      "tables": {
        "RAW_CUSTOMERS": {
          "columns": [
            {"name": "ID", "data_type": "NUMBER", "nullable": false},
            {"name": "INGESTED_AT", "data_type": "TIMESTAMP_NTZ", "nullable": false}
          ],
          "row_count": 5000,
          "watermarks": {"INGESTED_AT": "2026-09-18T10:45:00"}
        }
      }
    }
"""

from __future__ import annotations

from typing import Any

from dvp.connectors.base import ColumnDef, Connector, HealthResult, QueryResult, TableRef
from dvp.core.errors import NotFoundError


class InlineConnector(Connector):
    dialect = "sqlite"

    def __init__(
        self, coordinates: dict, auth_method: str = "none", secret: dict | None = None
    ) -> None:
        self.coordinates = coordinates or {}
        self._tables: dict[str, dict] = {
            k.upper(): v for k, v in (self.coordinates.get("tables") or {}).items()
        }

    # ----- introspection -----
    def test(self) -> HealthResult:
        return HealthResult(
            ok=True,
            detail=f"inline source with {len(self._tables)} table(s)",
            category="ok",
        )

    def list_tables(self) -> list[str]:
        return sorted(self._tables.keys())

    def list_schemas(self) -> list[str]:
        return [self.coordinates.get("schema") or "main"]

    def list_tables_in_schema(self, schema: str) -> list[dict]:  # noqa: ARG002
        return [{"name": n, "type": "table"} for n in self.list_tables()]

    def _table(self, table: TableRef) -> dict:
        spec = self._tables.get(table.table.upper())
        if spec is None:
            raise NotFoundError("table not found in inline source", details={"table": table.table})
        return spec

    def get_schema(self, table: TableRef) -> list[ColumnDef]:
        spec = self._table(table)
        out: list[ColumnDef] = []
        for i, c in enumerate(spec.get("columns", [])):
            out.append(
                ColumnDef(
                    name=c["name"],
                    data_type=c.get("data_type", "STRING"),
                    nullable=bool(c.get("nullable", True)),
                    ordinal=i + 1,
                )
            )
        return out

    def primary_key(self, table: TableRef) -> list[str]:
        """Declared PK from the stored spec (`primary_key` or `key`)."""
        spec = self._table(table)
        pk = spec.get("primary_key") or spec.get("key") or []
        return list(pk) if isinstance(pk, (list, tuple)) else [str(pk)]

    def metadata_row_count(self, table: TableRef) -> int | None:
        return int(self._table(table).get("row_count", 0))

    def count(self, table: TableRef, predicate: str | None = None) -> int:
        spec = self._table(table)
        return int(spec.get("row_count", 0))

    def latest_watermark(self, table: TableRef, column: str) -> str | None:
        """Max value of the watermark column (duck-typed; used by the profiler)."""
        spec = self._table(table)
        wms = {k.upper(): v for k, v in (spec.get("watermarks") or {}).items()}
        return wms.get(column.upper())

    def sample_rows(self, table: TableRef, limit: int = 20) -> tuple[list[str], list[tuple]]:
        """Preview rows straight from the stored spec (inline source can't run SQL)."""
        spec = self._table(table)
        cols = [c["name"] for c in spec.get("columns", [])]
        rows = spec.get("rows") or []
        out = [tuple(r.get(c) for c in cols) for r in rows[: max(0, limit)]]
        return cols, out

    def execute(self, sql: str) -> QueryResult:  # pragma: no cover - not used locally
        raise NotImplementedError("InlineConnector does not support arbitrary SQL")

    def spark_reader(self, spark: Any, table: TableRef, partition: dict | None = None) -> Any:
        raise NotImplementedError("InlineConnector has no Spark reader")
