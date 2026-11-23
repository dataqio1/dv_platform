"""OracleConnector dialect helpers + wiring (no live DB needed).

Live Oracle↔Snowflake compares are exercised by tests/scripts/test_oracle_live.sh when
ORACLE_* env vars point at a real instance."""

from __future__ import annotations

import pytest

from dvp.connectors.base import TableRef
from dvp.connectors.oracle import OracleConnector
from dvp.connectors.registry import build_connector
from dvp.core.errors import DVPError


def test_registry_builds_oracle():
    assert isinstance(build_connector("oracle", {}, "password", None), OracleConnector)


def test_sql_ref_is_owner_dot_table():
    c = OracleConnector({}, "password", {})
    assert c.sql_ref(TableRef("ORCL", "HR", "EMPLOYEES")) == '"HR"."EMPLOYEES"'
    # falls back to database when schema is absent
    assert c.sql_ref(TableRef("HR", "", "EMP")) == '"HR"."EMP"'


def test_sample_sql_uses_rownum_wrapper():
    c = OracleConnector({}, "password", {})
    assert c.sample_sql('SELECT * FROM "HR"."EMP"', 2000) == (
        'SELECT * FROM (SELECT * FROM "HR"."EMP") WHERE ROWNUM <= 2000'
    )


def test_connect_requires_host_or_dsn():
    c = OracleConnector({}, "password", {"user": "HR", "password": "x"})
    with pytest.raises(DVPError, match="host"):
        c._connect()


def test_connect_requires_service_or_sid():
    c = OracleConnector({"host": "db.example.com"}, "password", {"user": "HR", "password": "x"})
    with pytest.raises(DVPError, match="service_name or sid"):
        c._connect()
