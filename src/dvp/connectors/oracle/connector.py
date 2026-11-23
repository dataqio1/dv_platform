"""OracleConnector — real implementation over python-oracledb (thin mode).

Pushdown-first like the Snowflake connector: `execute()` runs SQL in Oracle and returns
result rows; `count()`/`get_schema()` use metadata queries (`ALL_TAB_COLUMNS`).

Oracle dialect specifics (matching the datadiff Oracle path):
* addressing is **OWNER.TABLE** — no catalog/`database.schema.table` (so `sql_ref` emits
  `"OWNER"."TABLE"`);
* no `LIMIT` — sampling uses a `ROWNUM` wrapper (`sample_sql`);
* scalar selects come `FROM DUAL`.

Thin mode needs no Oracle Instant Client. Install the `oracle` extra (`oracledb`).
"""

from __future__ import annotations

from typing import Any

from dvp.connectors.base import ColumnDef, Connector, HealthResult, QueryResult, TableRef
from dvp.core.errors import AuthError, DVPError


class OracleConnector(Connector):
    dialect = "oracle"

    def __init__(self, coordinates: dict, auth_method: str, secret: dict | None) -> None:
        self.coordinates = coordinates or {}
        self.auth_method = auth_method
        self.secret = secret or {}

    def server_fingerprint(self) -> str:
        """Same host/port/service(or dsn) ⇒ same server; owners/tables differ by FQN."""
        c = self.coordinates
        key = (
            c.get("dsn")
            or f"{c.get('host')}:{c.get('port', 1521)}/{c.get('service_name') or c.get('sid')}"
        )
        return f"oracle://{str(key).lower()}"

    def _connect(self):
        try:
            import oracledb
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "python-oracledb not installed. Install the 'oracle' extra."
            ) from exc
        c = self.coordinates
        dsn = c.get("dsn")
        if not dsn:
            host = c.get("host")
            port = int(c.get("port", 1521))
            service = c.get("service_name") or c.get("service")
            sid = c.get("sid")
            if not host:
                raise DVPError("Oracle connection needs a host (or a full dsn)")
            if service:
                dsn = oracledb.makedsn(host, port, service_name=service)
            elif sid:
                dsn = oracledb.makedsn(host, port, sid=sid)
            else:
                raise DVPError("Oracle connection needs service_name or sid (or a dsn)")
        user = self.secret.get("user") or self.secret.get("username") or c.get("user")
        password = self.secret.get("password")
        try:
            conn = oracledb.connect(user=user, password=password, dsn=dsn)
        except oracledb.Error as exc:
            msg = str(exc)
            low = msg.lower()
            if "ora-01017" in low or "invalid username" in low or "logon denied" in low:
                raise AuthError(f"Oracle authentication failed: {msg}") from exc
            raise DVPError(f"Oracle connection error: {msg}") from exc
        self._pin_session(conn)
        return conn

    @staticmethod
    def _pin_session(conn) -> None:
        """Pin time zone to UTC so TIMESTAMP WITH [LOCAL] TIME ZONE renders deterministically, plus
        deterministic NLS date/timestamp text — the cross-engine compare canonical
        (sql_canon.temporal_text) assumes both sides render in UTC, else equal instants hash
        differently. Best-effort: a session that rejects these (rare) stays usable."""
        try:
            cur = conn.cursor()
            try:
                cur.execute("ALTER SESSION SET TIME_ZONE = 'UTC'")
                cur.execute("ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS'")
                cur.execute("ALTER SESSION SET NLS_TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF6'")
                cur.execute(
                    "ALTER SESSION SET NLS_TIMESTAMP_TZ_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF6 TZR'"
                )
            finally:
                cur.close()
        except Exception:  # noqa: BLE001 - session tuning is best-effort, never fatal
            pass

    def test(self) -> HealthResult:
        try:
            conn = self._connect()
        except AuthError as exc:
            return HealthResult(ok=False, detail=exc.message, category="auth")
        except DVPError as exc:
            return HealthResult(ok=False, detail=exc.message, category="network")
        try:
            cur = conn.cursor()
            cur.execute("SELECT 'ok' FROM DUAL")
            cur.fetchone()
            version = getattr(conn, "version", "unknown")
            return HealthResult(ok=True, detail=f"connected to Oracle {version}", category="ok")
        finally:
            conn.close()

    # ----- dialect helpers (duck-typed, used by evaluator/compare) -----
    def sql_ref(self, table: TableRef) -> str:
        owner = table.schema or table.database
        return f'"{owner}"."{table.table}"' if owner else f'"{table.table}"'

    def sample_sql(self, base_sql: str, n: int) -> str:
        """Oracle has no LIMIT; bound rows with a ROWNUM wrapper."""
        return f"SELECT * FROM ({base_sql}) WHERE ROWNUM <= {int(n)}"

    # ----- introspection / execution -----
    def execute(self, sql: str) -> QueryResult:
        conn = self._connect()
        try:
            cur = conn.cursor()
            # Bulk-fetch tuning: python-oracledb defaults to arraysize=100 / prefetchrows=2, i.e.
            # ~1 network round-trip per 100 rows. Over a remote (cross-cloud) DB that dominates the
            # streaming compare's wall-clock — a 50k-row window becomes ~500 round-trips. Fetch in
            # large batches instead. prefetchrows must be set before execute() to take effect.
            cur.prefetchrows = 10000
            cur.arraysize = 10000
            cur.execute(sql)
            cols = [d[0] for d in (cur.description or [])]
            rows = cur.fetchall()
            return QueryResult(columns=cols, rows=rows)
        finally:
            conn.close()

    def count(self, table: TableRef, predicate: str | None = None) -> int:
        where = f" WHERE {predicate}" if predicate else ""
        res = self.execute(f"SELECT COUNT(*) FROM {self.sql_ref(table)}{where}")
        return int(res.rows[0][0])

    def get_schema(self, table: TableRef) -> list[ColumnDef]:
        owner = (table.schema or table.database or "").upper()
        res = self.execute(
            "SELECT column_name, data_type, nullable, column_id "
            "FROM all_tab_columns "
            f"WHERE owner = '{owner}' AND table_name = '{table.table.upper()}' "
            "ORDER BY column_id"
        )
        return [
            ColumnDef(name=r[0], data_type=r[1], nullable=(r[2] == "Y"), ordinal=int(r[3] or 0))
            for r in res.rows
        ]

    def metadata_row_count(self, table: TableRef) -> int | None:
        """Optimizer statistic. None until the table has been analyzed."""
        owner = (table.schema or table.database or "").upper()
        res = self.execute(
            "SELECT num_rows FROM all_tables "
            f"WHERE owner = '{owner}' AND table_name = '{table.table.upper()}'"
        )
        if not res.rows or res.rows[0][0] is None:
            return None
        return int(res.rows[0][0])

    def latest_watermark(self, table: TableRef, column: str) -> str | None:
        res = self.execute(f'SELECT MAX("{column}") FROM {self.sql_ref(table)}')
        v = res.rows[0][0] if res.rows else None
        if v is None:
            return None
        return v.isoformat() if hasattr(v, "isoformat") else str(v)

    # ----- catalog browsing (schema/owner → tables) -----
    def list_schemas(self) -> list[str]:
        # Oracle "schemas" are object owners; exclude common system accounts.
        res = self.execute(
            "SELECT username FROM all_users "
            "WHERE username NOT IN ('SYS','SYSTEM','OUTLN','XDB','DBSNMP','APPQOSSYS',"
            "'ORDS_METADATA','AUDSYS','GSMADMIN_INTERNAL','DBSFWUSER','REMOTE_SCHEDULER_AGENT') "
            "ORDER BY username"
        )
        return [r[0] for r in res.rows]

    def list_tables_in_schema(self, schema: str) -> list[dict]:
        owner = schema.upper()
        res = self.execute(
            f"SELECT table_name, 'TABLE' typ FROM all_tables WHERE owner = '{owner}' "
            f"UNION ALL SELECT view_name, 'VIEW' typ FROM all_views WHERE owner = '{owner}' "
            "ORDER BY 1"
        )
        return [{"name": r[0], "type": str(r[1]).lower()} for r in res.rows]

    def spark_reader(self, spark: Any, table: TableRef, partition: dict | None = None) -> Any:
        raise NotImplementedError("Spark reads land with the cross-DB compare path")
