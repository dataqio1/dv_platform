"""Session pinning — the Snowflake/Oracle connectors must force the session time zone to UTC so
the cross-engine compare canonical (sql_canon.temporal_text) renders equal instants identically on
both sides. These can't touch a live warehouse, so they stub ONLY the external driver module and
assert the real connector code wires the session the way we expect."""

from __future__ import annotations

import sys
import types

from dvp.connectors.oracle.connector import OracleConnector
from dvp.connectors.snowflake.connector import SnowflakeConnector


def test_snowflake_pins_session_timezone_utc(monkeypatch):
    captured: dict = {}

    fake_root = types.ModuleType("snowflake")
    fake_conn_mod = types.ModuleType("snowflake.connector")
    fake_conn_mod.errors = types.SimpleNamespace(DatabaseError=type("DatabaseError", (Exception,), {}))

    def _connect(**kwargs):
        captured.update(kwargs)
        return object()

    fake_conn_mod.connect = _connect
    fake_root.connector = fake_conn_mod
    monkeypatch.setitem(sys.modules, "snowflake", fake_root)
    monkeypatch.setitem(sys.modules, "snowflake.connector", fake_conn_mod)

    conn = SnowflakeConnector({"account": "acme", "user": "u"}, "external_browser", {})
    conn._connect()

    sp = captured["session_parameters"]
    assert sp["TIMEZONE"] == "UTC"
    assert sp["QUERY_TAG"] == "dvp"  # existing params preserved, not clobbered


def test_oracle_pins_session_timezone_utc(monkeypatch):
    executed: list[str] = []

    class _Cursor:
        def execute(self, sql):
            executed.append(sql)

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cursor()

    fake = types.ModuleType("oracledb")
    fake.Error = type("Error", (Exception,), {})
    fake.connect = lambda **kwargs: _Conn()
    monkeypatch.setitem(sys.modules, "oracledb", fake)

    conn = OracleConnector({"dsn": "host:1521/svc"}, "password", {"user": "u", "password": "p"})
    conn._connect()

    assert any("TIME_ZONE = 'UTC'" in s for s in executed)
    assert any("NLS_TIMESTAMP_FORMAT" in s and "FF6" in s for s in executed)


def test_oracle_session_tuning_is_best_effort(monkeypatch):
    # A session that rejects ALTER SESSION must not break the connection.
    class _Cursor:
        def execute(self, sql):
            raise RuntimeError("ORA-01031: insufficient privileges")

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cursor()

    fake = types.ModuleType("oracledb")
    fake.Error = type("Error", (Exception,), {})
    fake.connect = lambda **kwargs: _Conn()
    monkeypatch.setitem(sys.modules, "oracledb", fake)

    conn = OracleConnector({"dsn": "host:1521/svc"}, "password", {"user": "u", "password": "p"})
    assert conn._connect() is not None  # tuning failure swallowed, connection still returned
