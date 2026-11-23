"""Browser-SSO "auth once per process" guarantee.

Browser SSO pops a web page on every ``sf.connect()``. These tests stub the driver
and count connects to prove the process-level pool opens the connection ONCE per
identity and reuses it across connectors/compares — and that a dead session is
evicted so the next use rebuilds. No live warehouse is touched.
"""

from __future__ import annotations

import sys
import types

import pytest

from dvp.connectors.pool import POOL
from dvp.connectors.snowflake.connector import SnowflakeConnector

COORDS = {"account": "acme", "user": "u", "warehouse": "WH", "role": "R"}


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql):
        if self._conn.closed:
            raise self._conn.op_error("Connection is closed")
        self._conn.executed.append(sql)
        self._rows = [("v1", "acme", "u")] if "CURRENT_VERSION" in sql else [(1,)]
        self.description = [("A",)] * len(self._rows[0])

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0]

    def cancel(self):
        pass

    def close(self):
        pass


class _FakeConn:
    def __init__(self, op_error):
        self.closed = False
        self.executed: list[str] = []
        self.op_error = op_error

    def cursor(self):
        return _FakeCursor(self)

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True


def _install_fake_driver(monkeypatch) -> dict:
    """Stub snowflake.connector; return state with a connect counter (= popup count)."""
    state = {"connects": 0, "conns": []}

    class OperationalError(Exception):
        pass

    class InterfaceError(Exception):
        pass

    class DatabaseError(Exception):
        pass

    errors_mod = types.ModuleType("snowflake.connector.errors")
    errors_mod.OperationalError = OperationalError
    errors_mod.InterfaceError = InterfaceError
    errors_mod.DatabaseError = DatabaseError

    def _connect(**kwargs):
        state["connects"] += 1
        conn = _FakeConn(OperationalError)
        state["conns"].append(conn)
        return conn

    conn_mod = types.ModuleType("snowflake.connector")
    conn_mod.connect = _connect
    conn_mod.errors = errors_mod
    root = types.ModuleType("snowflake")
    root.connector = conn_mod

    monkeypatch.setitem(sys.modules, "snowflake", root)
    monkeypatch.setitem(sys.modules, "snowflake.connector", conn_mod)
    monkeypatch.setitem(sys.modules, "snowflake.connector.errors", errors_mod)
    return state


@pytest.fixture(autouse=True)
def _clean_pool():
    POOL.close_all()
    yield
    POOL.close_all()


def test_browser_sso_connects_once_across_connectors(monkeypatch):
    """Two connectors with the same identity (source + target of a compare, or two runs)
    share ONE physical connection → the browser pops once, not per connector."""
    state = _install_fake_driver(monkeypatch)

    a = SnowflakeConnector(COORDS, "external_browser", {})
    b = SnowflakeConnector(COORDS, "external_browser", {})
    a.execute("SELECT 1")
    b.execute("SELECT 1")

    assert state["connects"] == 1  # one popup total
    assert a._session() is b._session()  # literally the same connection object


def test_close_leaves_pooled_connection_open(monkeypatch):
    """Ending a compare must not close a pooled connection — the next compare would
    otherwise have to re-authenticate (another popup)."""
    state = _install_fake_driver(monkeypatch)

    a = SnowflakeConnector(COORDS, "external_browser", {})
    a.execute("SELECT 1")
    a.close()  # end-of-compare release
    assert state["conns"][0].closed is False  # still open in the pool

    b = SnowflakeConnector(COORDS, "external_browser", {})
    b.execute("SELECT 1")
    assert state["connects"] == 1  # reused, no new popup


def test_dead_session_is_evicted_and_rebuilt(monkeypatch):
    """A broken pooled connection is evicted so the next use rebuilds (one unavoidable
    re-auth) instead of handing a dead connection to the next compare."""
    state = _install_fake_driver(monkeypatch)

    a = SnowflakeConnector(COORDS, "external_browser", {})
    a.execute("SELECT 1")
    state["conns"][0].close()  # simulate the warehouse dropping the session

    a.execute("SELECT 1")  # hits connection-closed, evicts, reconnects, retries
    assert state["connects"] == 2

    b = SnowflakeConnector(COORDS, "external_browser", {})
    b.execute("SELECT 1")
    assert state["connects"] == 2  # the rebuilt connection is the one now pooled/reused


def test_key_pair_auth_is_not_pooled(monkeypatch):
    """Key-pair auth never pops a browser, so it keeps the original private-session
    behavior — no cross-connector sharing."""
    _install_fake_driver(monkeypatch)

    a = SnowflakeConnector(COORDS, "key_pair", {"private_key": "x"})
    # key_pair would normally load a real key; stub build_connect_kwargs out of the way
    monkeypatch.setattr(
        "dvp.connectors.snowflake.connector.build_connect_kwargs",
        lambda coords, auth, secret: {"account": coords["account"], "session_parameters": {}},
    )
    a.execute("SELECT 1")
    assert a._entry is None  # not pooled
