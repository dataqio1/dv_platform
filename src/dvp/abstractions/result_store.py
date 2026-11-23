"""ResultStore — where cell-level diffs & bad-record samples (which contain real
data values) are persisted. Pluggable per group so sensitive values can stay inside
the customer's own Snowflake (docs/design/05-data-testing.md §8.1, decision D9).

The summary always lives in the metadata DB; only heavy *detail* goes here, and
`detail_ref` is an opaque pointer (object-store URI, local path, or Snowflake ref).
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from dvp.core.tenancy import TenantContext


def _json_default(o: object):  # noqa: ANN202
    """Make warehouse cell values JSON-safe: Decimal (NUMBER) → float, datetime/date → ISO,
    bytes → text. Without this a compare over NUMBER/TIMESTAMP columns can't persist its diffs
    ('Object of type Decimal is not JSON serializable')."""
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (_dt.datetime, _dt.date, _dt.time)):
        return o.isoformat()
    if isinstance(o, bytes):
        return o.decode("utf-8", "replace")
    return str(o)


class ResultStore(Protocol):
    def write_detail(self, ctx: TenantContext, key: str, rows: list[dict]) -> str:
        """Persist detail rows; return an opaque `detail_ref`."""
        ...

    def read_detail(
        self, ctx: TenantContext, ref: str, *, offset: int = 0, limit: int = 100
    ) -> list[dict]: ...

    def delete(self, ctx: TenantContext, ref: str) -> None:
        """Delete a detail artifact. Idempotent (a missing ref is a no-op) and tenant-
        scoped — a group may only delete artifacts under its own prefix. Called when a
        test case / batch is deleted so stored values never outlive their test case."""
        ...


class LocalFileResultStore:
    """Writes newline-delimited JSON under a per-group directory. Desktop/local mode."""

    def __init__(self, base_dir: str) -> None:
        self.base = Path(base_dir)

    def _path(self, ctx: TenantContext, key: str) -> Path:
        return self.base / ctx.org_id / ctx.group_id / f"{key}.ndjson"

    def write_detail(self, ctx: TenantContext, key: str, rows: list[dict]) -> str:
        p = self._path(ctx, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, default=_json_default) + "\n")
        return f"file://{p.resolve()}"

    def _scoped_path(self, ctx: TenantContext, ref: str) -> Path:
        """Resolve a ref to a path, enforcing that it lives under the caller's own
        org/group directory (tenant isolation for both read and delete)."""
        path = Path(ref.removeprefix("file://")).resolve()
        group_root = (self.base / ctx.org_id / ctx.group_id).resolve()
        if group_root not in path.parents and path.parent != group_root:
            from dvp.core.errors import PermissionDeniedError

            raise PermissionDeniedError("detail_ref is outside the caller's group scope")
        return path

    def read_detail(
        self, ctx: TenantContext, ref: str, *, offset: int = 0, limit: int = 100
    ) -> list[dict]:
        path = self._scoped_path(ctx, ref)
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as fh:
            lines = fh.readlines()[offset : offset + limit]
        return [json.loads(line) for line in lines]

    def delete(self, ctx: TenantContext, ref: str) -> None:
        path = self._scoped_path(ctx, ref)
        path.unlink(missing_ok=True)


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_ident(name: str) -> str:
    if not _IDENT.match(name or ""):
        from dvp.core.errors import ValidationError

        raise ValidationError(f"invalid identifier: {name}")
    return f'"{name}"'


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class WarehouseRef:
    def __init__(
        self, connection_id: str, database: str, schema: str, table: str, sample_key: str
    ) -> None:
        self.connection_id = connection_id
        self.database = database
        self.schema = schema
        self.table = table
        self.sample_key = sample_key

    @property
    def location(self) -> str:
        return f"{self.database}.{self.schema}.{self.table}"


def parse_warehouse_ref(ref: str) -> WarehouseRef | None:
    """warehouse://{connection}/{database}.{schema}.{table}/{group_id}/{key}"""
    if not ref or not ref.startswith("warehouse://"):
        return None
    rest = ref.removeprefix("warehouse://")
    parts = rest.split("/", 3)
    if len(parts) != 4:
        return None
    connection_id, location, group_id, key = parts
    bits = location.split(".")
    if len(bits) != 3 or not all(bits):
        return None
    return WarehouseRef(connection_id, bits[0], bits[1], bits[2], f"{group_id}/{key}")


class WarehouseResultStore:
    """Writes compare samples into a table the customer owns.

    The returned ref is a pointer. The row values live only in that table.
    """

    def __init__(
        self,
        connector,  # noqa: ANN001
        *,
        connection_id: str,
        database: str,
        schema: str,
        table: str,
    ) -> None:
        self.connector = connector
        self.connection_id = connection_id
        self.database = database
        self.schema = schema
        self.table = table

    def _qualified(self) -> str:
        if getattr(self.connector, "dialect", "") == "snowflake":
            return ".".join(quote_ident(p) for p in (self.database, self.schema, self.table))
        return quote_ident(self.table)

    def _ensure(self) -> str:
        qualified = self._qualified()
        self.connector.execute(
            f"CREATE TABLE IF NOT EXISTS {qualified} ("
            "SAMPLE_KEY VARCHAR, ROW_INDEX INTEGER, PAYLOAD VARCHAR)"
        )
        return qualified

    def _ref(self, ctx: TenantContext, key: str) -> str:
        sample_key = key if key.startswith(f"{ctx.group_id}/") else f"{ctx.group_id}/{key}"
        location = f"{self.database}.{self.schema}.{self.table}"
        return f"warehouse://{self.connection_id}/{location}/{sample_key}"

    def write_detail(self, ctx: TenantContext, key: str, rows: list[dict]) -> str:
        qualified = self._ensure()
        ref = self._ref(ctx, key)
        parsed = parse_warehouse_ref(ref)
        assert parsed is not None
        sample_key = _sql_str(parsed.sample_key)
        self.connector.execute(f"DELETE FROM {qualified} WHERE SAMPLE_KEY = {sample_key}")
        if rows:
            values = ", ".join(
                f"({sample_key}, {i}, {_sql_str(json.dumps(row, default=_json_default))})"
                for i, row in enumerate(rows)
            )
            self.connector.execute(
                f"INSERT INTO {qualified} (SAMPLE_KEY, ROW_INDEX, PAYLOAD) VALUES {values}"
            )
        return ref

    def _owned(self, ctx: TenantContext, ref: str) -> WarehouseRef:
        from dvp.core.errors import PermissionDeniedError, ValidationError

        parsed = parse_warehouse_ref(ref)
        if parsed is None:
            raise ValidationError("sample pointer is not a warehouse location")
        if not parsed.sample_key.startswith(f"{ctx.group_id}/"):
            raise PermissionDeniedError("sample pointer is outside this group")
        if parsed.connection_id != self.connection_id:
            raise PermissionDeniedError("sample pointer is outside this group")
        return parsed

    def read_detail(
        self, ctx: TenantContext, ref: str, *, offset: int = 0, limit: int = 100
    ) -> list[dict]:
        parsed = self._owned(ctx, ref)
        qualified = self._qualified()
        offset = max(0, int(offset))
        limit = max(0, int(limit))
        res = self.connector.execute(
            "SELECT PAYLOAD FROM "
            f"{qualified} WHERE SAMPLE_KEY = {_sql_str(parsed.sample_key)} "
            f"ORDER BY ROW_INDEX LIMIT {limit} OFFSET {offset}"
        )
        out = []
        for row in res.rows:
            raw = row[0]
            out.append(json.loads(raw) if isinstance(raw, str) else raw)
        return out

    def delete(self, ctx: TenantContext, ref: str) -> None:
        parsed = self._owned(ctx, ref)
        qualified = self._qualified()
        self.connector.execute(
            f"DELETE FROM {qualified} WHERE SAMPLE_KEY = {_sql_str(parsed.sample_key)}"
        )
