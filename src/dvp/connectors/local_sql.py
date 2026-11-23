"""LocalSqlConnector — a real SQL source backed by in-memory SQLite.

Unlike `InlineConnector` (schema + counts only), this loads actual **rows** from the
connection's coordinates into an in-memory SQLite database and answers real SQL via
`execute()`. That lets the pushdown rule evaluator and profiler run genuine SQL locally
(no warehouse, no mocks) over the exact code path used against Snowflake in production.

Coordinates shape::

    {
      "database": "RAW_SALES", "schema": "PUBLIC",
      "tables": {
        "RAW_ORDERS": {
          "columns": [{"name":"ORDER_ID","data_type":"NUMBER","nullable":false},
                      {"name":"AMOUNT","data_type":"NUMBER","nullable":false},
                      {"name":"INGESTED_AT","data_type":"TIMESTAMP_NTZ","nullable":false}],
          "rows": [{"ORDER_ID":1,"AMOUNT":10,"INGESTED_AT":"2026-09-20T02:00:00"}, ...]
        }
      }
    }
"""

from __future__ import annotations

import sqlite3
from typing import Any

from dvp.connectors.base import ColumnDef, Connector, HealthResult, QueryResult, TableRef
from dvp.core.errors import NotFoundError


class LocalSqlConnector(Connector):
    dialect = "sqlite"

    def __init__(
        self, coordinates: dict, auth_method: str = "none", secret: dict | None = None
    ) -> None:
        self.coordinates = coordinates or {}
        self._specs: dict[str, dict] = {
            k.upper(): v for k, v in (self.coordinates.get("tables") or {}).items()
        }
        # A file path keeps tables across connector instances. Sample storage uses it so a
        # later read sees rows written by an earlier request. Omitted means a fresh memory DB.
        path = self.coordinates.get("sqlite_path")
        self._conn = sqlite3.connect(path or ":memory:", check_same_thread=False)
        self._conn.create_function("REGEXP", 2, _regexp)
        # Deterministic 64-bit row hash so the recursive hash-compare engine (which pushes
        # HASH() into the DB) can also run on the local SQLite source for dev + tests.
        self._conn.create_function("DVHASH", 1, _dvhash)
        self._load()

    def _load(self) -> None:
        cur = self._conn.cursor()
        for name, spec in self._specs.items():
            cols = [c["name"] for c in spec.get("columns", [])]
            if not cols:
                continue
            col_ddl = ", ".join(f'"{c}"' for c in cols)
            cur.execute(f'CREATE TABLE "{name}" ({col_ddl})')
            rows = spec.get("rows", [])
            if rows:
                placeholders = ", ".join("?" for _ in cols)
                cur.executemany(
                    f'INSERT INTO "{name}" ({col_ddl}) VALUES ({placeholders})',
                    [[r.get(c) for c in cols] for r in rows],
                )
        self._conn.commit()

    # ----- identity / introspection -----
    def test(self) -> HealthResult:
        return HealthResult(
            ok=True, detail=f"local sqlite with {len(self._specs)} table(s)", category="ok"
        )

    def list_tables(self) -> list[str]:
        return sorted(self._specs.keys())

    def list_schemas(self) -> list[str]:
        return [self.coordinates.get("schema") or "main"]

    def list_tables_in_schema(self, schema: str) -> list[dict]:  # noqa: ARG002
        return [{"name": n, "type": "table"} for n in self.list_tables()]

    def sql_ref(self, table: TableRef) -> str:
        """How this source names the table inside SQL (single-part for SQLite)."""
        return f'"{table.table.upper()}"'

    def _spec(self, table: TableRef) -> dict:
        spec = self._specs.get(table.table.upper())
        if spec is None:
            raise NotFoundError("table not found in local source", details={"table": table.table})
        return spec

    def last_altered(self, table: TableRef) -> str | None:
        """Warehouse-native last-change time (freshness for tables with no load column). The local
        source has no real DDL/DML clock, so it returns a `last_altered` seeded in the table spec
        (lets the warehouse-freshness path be tested without a live warehouse)."""
        return self._spec(table).get("last_altered")

    def columns_for_schema(self, schema: str, database: str | None = None) -> dict:  # noqa: ARG002
        """Every table's columns at once (batched-fetch parity with the warehouse connectors)."""
        out: dict[str, list[ColumnDef]] = {}
        for name, spec in self._specs.items():
            out[name] = [
                ColumnDef(name=c["name"], data_type=c.get("data_type", "STRING"),
                          nullable=bool(c.get("nullable", True)), ordinal=i + 1)
                for i, c in enumerate(spec.get("columns", []))
            ]
        return out

    def get_schema(self, table: TableRef) -> list[ColumnDef]:
        spec = self._spec(table)
        return [
            ColumnDef(
                name=c["name"],
                data_type=c.get("data_type", "STRING"),
                nullable=bool(c.get("nullable", True)),
                ordinal=i + 1,
            )
            for i, c in enumerate(spec.get("columns", []))
        ]

    def execute(self, sql: str) -> QueryResult:
        cur = self._conn.cursor()
        cur.execute(sql)
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchall() if cur.description else []
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if head not in {"SELECT", "WITH", "EXPLAIN", "PRAGMA"}:
            self._conn.commit()
        return QueryResult(columns=cols, rows=rows)

    def primary_key(self, table: TableRef) -> list[str]:
        """Declared PK columns from SQLite's table_info (pk>0, in key order)."""
        rows = self.execute(f"PRAGMA table_info({self.sql_ref(table)})").rows
        # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
        pk = [(r[5], r[1]) for r in rows if len(r) > 5 and r[5]]
        return [name for _, name in sorted(pk)]

    def count(self, table: TableRef, predicate: str | None = None) -> int:
        where = f" WHERE {predicate}" if predicate else ""
        return int(self.execute(f"SELECT COUNT(*) FROM {self.sql_ref(table)}{where}").rows[0][0])

    def latest_watermark(self, table: TableRef, column: str) -> str | None:
        res = self.execute(f'SELECT MAX("{column}") FROM {self.sql_ref(table)}')
        return res.rows[0][0] if res.rows else None

    def spark_reader(self, spark: Any, table: TableRef, partition: dict | None = None) -> Any:
        raise NotImplementedError("LocalSqlConnector has no Spark reader")


def _regexp(pattern: str, value: Any) -> int:
    import re

    if value is None:
        return 0
    return 1 if re.search(pattern, str(value)) else 0


def _dvhash(value: Any) -> int:
    """Deterministic signed 32-bit hash of a text value (mirrors a warehouse HASH()).

    32-bit keeps SUM() of many rows inside SQLite's 64-bit integer range; real warehouses use
    their native 64-bit HASH where the aggregate is a wide NUMBER and never overflows."""
    import hashlib

    h = hashlib.md5(str(value if value is not None else "").encode()).digest()  # noqa: S324
    return int.from_bytes(h[:4], "big") - (1 << 31)  # signed 32-bit
