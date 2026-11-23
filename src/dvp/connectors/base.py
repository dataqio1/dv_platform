"""Connector + DialectAdapter protocols (docs/design/05 §10, 03 §1).

Concrete Snowflake/Oracle connectors (optional extras) implement these. The
observability engine uses `execute` for pushdown aggregates; the compare engine
uses `count`, `get_schema`, and `spark_reader`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class TableRef:
    database: str
    schema: str
    table: str

    def fqn(self, quote: str = '"') -> str:
        return ".".join(f"{quote}{p}{quote}" for p in (self.database, self.schema, self.table))


@dataclass
class ColumnDef:
    name: str
    data_type: str
    nullable: bool = True
    ordinal: int = 0


@dataclass
class HealthResult:
    ok: bool
    detail: str
    # Distinguish failure modes instead of a bare bool (docs/design/03 §7).
    category: str = "ok"  # ok | auth | network | warehouse | unknown


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]


class DialectAdapter(Protocol):
    name: str
    supports_except: bool
    supports_is_distinct_from: bool

    def quote(self, identifier: str) -> str: ...
    def count_sql(self, table: TableRef, predicate: str | None) -> str: ...
    def canonical_type(self, raw_type: str) -> str: ...


class Connector(Protocol):
    """The single contract every data source implements. All platform capabilities —
    count compare, schema compare, data validation, and data quality (rule evaluation +
    profiling) — are written once against this protocol, so they work on *any* source
    (Snowflake, Oracle, files/S3, …) with no application-level per-source branching.

    `dialect` lets a source own its SQL quirks (e.g. regex/limit syntax) so the engine
    stays source-agnostic. Sources that can't speak SQL natively (files) back themselves
    with an embedded engine so `execute`/`count`/`get_schema` still hold.
    """

    dialect: str  # e.g. "snowflake" | "oracle" | "postgres" | "sqlite"

    def test(self) -> HealthResult: ...
    def get_schema(self, table: TableRef) -> list[ColumnDef]: ...
    def count(self, table: TableRef, predicate: str | None = None) -> int: ...
    def execute(self, sql: str) -> QueryResult: ...
    def spark_reader(self, spark: Any, table: TableRef, partition: dict | None = None) -> Any: ...
