"""Build a Connector from a stored connection + secret."""

from __future__ import annotations

from dvp.connectors.base import Connector


def build_connector(
    source_type: str, coordinates: dict, auth_method: str, secret: dict | None
) -> Connector:
    if source_type == "snowflake":
        from dvp.connectors.snowflake import SnowflakeConnector

        return SnowflakeConnector(coordinates, auth_method, secret)
    if source_type == "inline":
        # Dependency-free local/demo source; schema & counts live in coordinates.
        from dvp.connectors.inline import InlineConnector

        return InlineConnector(coordinates, auth_method, secret)
    if source_type in ("file", "s3"):
        # CSV/JSON/NDJSON/Parquet from local path or S3, loaded into in-memory SQLite.
        from dvp.connectors.file_source import FileConnector

        return FileConnector(coordinates, auth_method, secret)
    if source_type in ("local", "local_sql", "sqlite"):
        # Real SQL over in-memory SQLite loaded from coordinates.rows — lets the
        # pushdown evaluator/profiler run genuine SQL locally (no warehouse).
        from dvp.connectors.local_sql import LocalSqlConnector

        return LocalSqlConnector(coordinates, auth_method, secret)
    if source_type == "oracle":
        from dvp.connectors.oracle import OracleConnector

        return OracleConnector(coordinates, auth_method, secret)
    raise ValueError(f"unknown source_type: {source_type}")
