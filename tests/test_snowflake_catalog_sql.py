"""Snowflake catalog/schema SQL must be database-qualified.

Regression guard for the bug where get_schema()/list_schemas()/list_tables_in_schema()
referenced an *unqualified* INFORMATION_SCHEMA. Snowflake scopes INFORMATION_SCHEMA to the
session's current database, so for any table in a different database than the connection
default those queries silently returned zero rows — which made watermark auto-detect come
back empty (no error, no candidates).

We can't reach a live warehouse in the suite, so we capture the SQL at the connector's
execute() boundary (the only place that talks to Snowflake) and assert it targets the
requested database. This runs with no credentials and would have failed before the fix.
"""

from __future__ import annotations

from dvp.connectors.base import QueryResult, TableRef
from dvp.connectors.snowflake.connector import SnowflakeConnector

OTHER_DB = "DWH_TEST"  # deliberately different from the connection's default database
COORDS = {"account": "acct", "database": "SNOWFLAKE_SAMPLE_DATA", "schema": "PUBLIC"}


def _recording_connector(rows: list[tuple], columns: list[str]):
    """A SnowflakeConnector whose execute() records SQL and returns canned rows instead
    of hitting a warehouse."""
    conn = SnowflakeConnector(COORDS, "key_pair", {"private_key": "x"})
    captured: list[str] = []

    def fake_execute(sql: str) -> QueryResult:
        captured.append(sql)
        return QueryResult(columns=columns, rows=rows)

    conn.execute = fake_execute  # type: ignore[method-assign]
    return conn, captured


def test_get_schema_is_qualified_by_the_tables_database():
    conn, sql = _recording_connector([], ["column_name", "data_type", "is_nullable", "ordinal"])
    conn.get_schema(TableRef(database=OTHER_DB, schema="DMS_BRONZE", table="BRZ_CUSTOMERS"))
    assert len(sql) == 1
    # Qualified with the table's database — not the bare, current-database form that
    # silently returned no rows for any other database.
    assert f'FROM "{OTHER_DB}".INFORMATION_SCHEMA.COLUMNS' in sql[0]
    assert "FROM information_schema.columns" not in sql[0].lower()


def test_list_schemas_honors_database_override():
    conn, sql = _recording_connector([], ["SCHEMA_NAME"])
    conn.list_schemas(database=OTHER_DB)
    assert f'"{OTHER_DB}".INFORMATION_SCHEMA.SCHEMATA' in sql[0]


def test_list_tables_in_schema_honors_database_override():
    conn, sql = _recording_connector([], ["TABLE_NAME", "TABLE_TYPE"])
    conn.list_tables_in_schema("DMS_BRONZE", database=OTHER_DB)
    assert f'"{OTHER_DB}".INFORMATION_SCHEMA.TABLES' in sql[0]


def test_catalog_falls_back_to_connection_default_database():
    """With no explicit database, queries target the connection's configured database."""
    conn, sql = _recording_connector([], ["SCHEMA_NAME"])
    conn.list_schemas()
    assert f'"{COORDS["database"]}".INFORMATION_SCHEMA.SCHEMATA' in sql[0]
