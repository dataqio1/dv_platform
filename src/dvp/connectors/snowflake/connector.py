"""SnowflakeConnector — real implementation over snowflake-connector-python.

Pushdown-first: execute() runs aggregate SQL in Snowflake and returns only the
result rows; count()/get_schema() use metadata queries. Auth is delegated to
auth.build_connect_kwargs (key-pair / OAuth / external-browser).
"""

from __future__ import annotations

from typing import Any

from dvp.connectors.base import ColumnDef, Connector, HealthResult, QueryResult, TableRef
from dvp.connectors.snowflake.auth import build_connect_kwargs
from dvp.core.errors import AuthError, AuthExpiredError, DVPError


class SnowflakeConnector(Connector):
    dialect = "snowflake"

    def __init__(self, coordinates: dict, auth_method: str, secret: dict | None) -> None:
        self.coordinates = coordinates
        self.auth_method = auth_method
        self.secret = secret or {}
        # Server-side guard: Snowflake aborts any single query that runs longer than this, so a
        # runaway compare can't keep a query executing for hours — even if the client thread is
        # killed. Platform default (overridable per-compare by the engine via this attribute).
        import os
        self.statement_timeout_s = int(
            coordinates.get("statement_timeout_s") or os.environ.get("DVP_SF_STATEMENT_TIMEOUT_S") or 3600
        )
        self._live = None  # reused session across queries (a compare issues many)
        self._active_cursor = None  # the currently-executing cursor, so a cancel can abort it

    def server_fingerprint(self) -> str:
        """Same account ⇒ same server (one session can query both tables). Database/schema
        can differ — those are part of the fully-qualified table name."""
        c = self.coordinates or {}
        return f"snowflake://{(c.get('account') or '').lower()}"

    def _connect(self):
        try:
            import snowflake.connector as sf
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "snowflake-connector-python not installed. Install the 'snowflake' extra."
            ) from exc
        kwargs = build_connect_kwargs(self.coordinates, self.auth_method, self.secret)
        sp = dict(kwargs.get("session_parameters") or {})
        # Pin the session time zone so TIMESTAMP_LTZ renders deterministically (UTC) regardless of
        # the account/user default — the cross-engine compare canonical (sql_canon.temporal_text)
        # assumes both sides render in UTC, else equal instants hash differently.
        sp.setdefault("TIMEZONE", "UTC")
        if self.statement_timeout_s and self.statement_timeout_s > 0:
            sp.setdefault("STATEMENT_TIMEOUT_IN_SECONDS", int(self.statement_timeout_s))
        kwargs["session_parameters"] = sp
        try:
            return sf.connect(**kwargs)
        except sf.errors.DatabaseError as exc:
            msg = str(exc)
            low = msg.lower()
            if "token" in low or "expired" in low or "oauth" in low:
                raise AuthExpiredError(f"Snowflake OAuth token invalid/expired: {msg}") from exc
            if "incorrect username or password" in low or "authentication" in low:
                raise AuthError(f"Snowflake authentication failed: {msg}") from exc
            raise DVPError(f"Snowflake connection error: {msg}") from exc

    def test(self) -> HealthResult:
        try:
            conn = self._connect()
        except AuthExpiredError as exc:
            return HealthResult(ok=False, detail=exc.message, category="auth")
        except AuthError as exc:
            return HealthResult(ok=False, detail=exc.message, category="auth")
        except DVPError as exc:
            return HealthResult(ok=False, detail=exc.message, category="network")
        try:
            cur = conn.cursor()
            cur.execute("SELECT CURRENT_VERSION(), CURRENT_ACCOUNT(), CURRENT_USER()")
            version, account, user = cur.fetchone()
            return HealthResult(
                ok=True,
                detail=f"connected to {account} as {user} (Snowflake {version})",
                category="ok",
            )
        finally:
            conn.close()

    def _session(self):
        """A reused live connection — a single compare issues hundreds of queries, so
        reconnecting each time added large overhead and spawned a new Snowflake session per
        query. Lazily (re)connects; callers must tolerate one reconnect via `execute`."""
        live = self._live
        if live is not None:
            try:
                if not live.is_closed():
                    return live
            except Exception:  # noqa: BLE001
                pass
        self._live = self._connect()
        return self._live

    def close(self) -> None:
        """Release the reused session (called after a compare finishes)."""
        if self._live is not None:
            try:
                self._live.close()
            except Exception:  # noqa: BLE001
                pass
            self._live = None

    def __del__(self):  # best-effort cleanup if close() wasn't called
        self.close()

    def cancel(self) -> None:
        """Abort the query currently running on this connection (called from another thread
        when the user cancels the execution). Best effort; Snowflake cursors are safe to
        cancel cross-thread."""
        cur = self._active_cursor
        if cur is not None:
            try:
                cur.cancel()
            except Exception:  # noqa: BLE001
                pass

    def _run(self, conn, sql: str) -> QueryResult:  # noqa: ANN001
        cur = conn.cursor()
        self._active_cursor = cur
        try:
            cur.execute(sql)
            cols = [c[0] for c in (cur.description or [])]
            return QueryResult(columns=cols, rows=cur.fetchall())
        finally:
            self._active_cursor = None
            cur.close()

    @staticmethod
    def _is_connection_error(exc: Exception) -> bool:
        """A dropped/closed session (worth one reconnect) — NOT a query error (bad SQL or a
        statement timeout), which we must never silently re-run (it would double the work)."""
        try:
            import snowflake.connector.errors as e

            if isinstance(exc, (e.OperationalError, e.InterfaceError)):
                return True
        except ImportError:  # pragma: no cover
            pass
        msg = str(exc).lower()
        return "connection" in msg and ("closed" in msg or "reset" in msg or "broken" in msg)

    def execute(self, sql: str) -> QueryResult:
        try:
            return self._run(self._session(), sql)
        except Exception as exc:  # noqa: BLE001
            reuse_dropped = self._is_connection_error(exc)
            self.close()  # drop a possibly-bad session either way
            if reuse_dropped:
                return self._run(self._session(), sql)  # reconnect + retry once
            raise

    def count(self, table: TableRef, predicate: str | None = None) -> int:
        where = f" WHERE {predicate}" if predicate else ""
        res = self.execute(f"SELECT COUNT(*) FROM {table.fqn()}{where}")
        return int(res.rows[0][0])

    def get_schema(self, table: TableRef) -> list[ColumnDef]:
        # Snowflake's INFORMATION_SCHEMA is per-database: an unqualified reference only
        # sees the session's current database, so qualify it with the table's database
        # (otherwise a table in any other database silently returns zero columns).
        db = table.database
        src = f'"{db}".INFORMATION_SCHEMA.COLUMNS' if db else "INFORMATION_SCHEMA.COLUMNS"
        res = self.execute(
            "SELECT column_name, data_type, is_nullable, ordinal_position "
            f"FROM {src} "
            f"WHERE TABLE_SCHEMA = '{table.schema}' AND TABLE_NAME = '{table.table}' "
            "ORDER BY ordinal_position"
        )
        return [
            ColumnDef(name=r[0], data_type=r[1], nullable=(r[2] == "YES"), ordinal=int(r[3]))
            for r in res.rows
        ]

    def columns_for_schema(
        self, schema: str, database: str | None = None
    ) -> dict[str, list[ColumnDef]]:
        """Every table's columns for a whole schema in ONE query (vs a round-trip per table). Lets
        load-type detection fetch 100+ tables' schemas at once — the slow part of bulk detection."""
        db = database or self.coordinates.get("database")
        src = f'"{db}".INFORMATION_SCHEMA.COLUMNS' if db else "INFORMATION_SCHEMA.COLUMNS"
        sch = (schema or "").replace("'", "''")
        res = self.execute(
            "SELECT table_name, column_name, data_type, is_nullable, ordinal_position "
            f"FROM {src} WHERE TABLE_SCHEMA = '{sch}' ORDER BY table_name, ordinal_position"
        )
        out: dict[str, list[ColumnDef]] = {}
        for r in res.rows:
            out.setdefault(r[0], []).append(
                ColumnDef(name=r[1], data_type=r[2], nullable=(r[3] == "YES"), ordinal=int(r[4]))
            )
        return out

    def primary_key(self, table: TableRef) -> list[str]:
        """Declared primary-key columns, in key order. Empty if the table has no PK."""
        db = f'"{table.database}".' if table.database else ""
        res = self.execute(f'SHOW PRIMARY KEYS IN TABLE {db}"{table.schema}"."{table.table}"')
        cols = {c.lower(): i for i, c in enumerate(res.columns)}
        name_i = cols.get("column_name")
        seq_i = cols.get("key_sequence")
        if name_i is None:
            return []
        rows = sorted(res.rows, key=lambda r: (r[seq_i] if seq_i is not None else 0))
        return [r[name_i] for r in rows]

    def metadata_row_count(self, table: TableRef) -> int | None:
        """Warehouse estimate from INFORMATION_SCHEMA. None when the catalog has no figure."""
        db = table.database
        src = f'"{db}".INFORMATION_SCHEMA.TABLES' if db else "INFORMATION_SCHEMA.TABLES"
        res = self.execute(
            "SELECT ROW_COUNT FROM "
            f"{src} WHERE TABLE_SCHEMA = '{table.schema}' AND TABLE_NAME = '{table.table}'"
        )
        if not res.rows or res.rows[0][0] is None:
            return None
        return int(res.rows[0][0])

    def latest_watermark(self, table: TableRef, column: str) -> str | None:
        """MAX of the load watermark. None when the column is entirely null or the table is empty."""
        col = column.replace('"', '""')
        res = self.execute(f'SELECT MAX("{col}") FROM {table.fqn()}')
        val = res.rows[0][0] if res.rows else None
        if val is None:
            return None
        return val.isoformat() if hasattr(val, "isoformat") else str(val)

    def last_altered(self, table: TableRef) -> str | None:
        """Warehouse-native last-change time from INFORMATION_SCHEMA — the freshness signal for a
        table with no load/watermark column (curated/published layers). None if not found."""
        db = (table.database or self.coordinates.get("database") or "").replace('"', '""')
        src = f'"{db}".INFORMATION_SCHEMA.TABLES' if db else "INFORMATION_SCHEMA.TABLES"
        schema = (table.schema or "").replace("'", "''")
        name = (table.table or "").replace("'", "''")
        res = self.execute(
            f"SELECT LAST_ALTERED FROM {src} "
            f"WHERE TABLE_SCHEMA = '{schema}' AND TABLE_NAME = '{name}'"
        )
        val = res.rows[0][0] if res.rows else None
        if val is None:
            return None
        return val.isoformat() if hasattr(val, "isoformat") else str(val)

    # ----- catalog browsing (database → schema → tables/views) -----
    def list_databases(self) -> list[str]:
        """Every database the connection's role can read (read-only; USAGE-visible).
        The account reaches many databases — coordinates.database is only a default, so
        browsing is account-wide rather than pinned to one database."""
        res = self.execute("SHOW TERSE DATABASES")
        cols = [c.lower() for c in res.columns]
        idx = cols.index("name") if "name" in cols else 1
        return sorted({r[idx] for r in res.rows})

    def list_schemas(self, database: str | None = None) -> list[str]:
        db = database or self.coordinates.get("database")
        src = f'"{db}".INFORMATION_SCHEMA.SCHEMATA' if db else "INFORMATION_SCHEMA.SCHEMATA"
        res = self.execute(
            f"SELECT SCHEMA_NAME FROM {src} WHERE SCHEMA_NAME <> 'INFORMATION_SCHEMA' "
            "ORDER BY SCHEMA_NAME"
        )
        return [r[0] for r in res.rows]

    def list_tables_in_schema(self, schema: str, database: str | None = None) -> list[dict]:
        db = database or self.coordinates.get("database")
        src = f'"{db}".INFORMATION_SCHEMA.TABLES' if db else "INFORMATION_SCHEMA.TABLES"
        res = self.execute(
            f"SELECT TABLE_NAME, TABLE_TYPE FROM {src} "
            f"WHERE TABLE_SCHEMA = '{schema}' ORDER BY TABLE_NAME"
        )
        return [
            {"name": r[0], "type": "view" if "VIEW" in (r[1] or "").upper() else "table"}
            for r in res.rows
        ]

    def spark_reader(self, spark: Any, table: TableRef, partition: dict | None = None) -> Any:
        raise NotImplementedError("Spark reads land with the cross-DB compare path")
