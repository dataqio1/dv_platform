"""FileConnector — read files (CSV / JSON / NDJSON / Parquet) from a local path or S3
and expose them through the standard SQL connector interface.

Files are parsed, type-inferred, and loaded into an in-memory SQLite database (via
`LocalSqlConnector`), so a file source supports the exact same `execute`/`count`/
`get_schema`/`sql_ref` surface as a warehouse — meaning the pushdown evaluator *and* the
data-compare engine work against files for free (compare a file vs a table, or two files).

Coordinates (single file)::

    {"uri": "s3://bucket/path/orders.csv", "format": "csv", "table": "ORDERS",
     "options": {"delimiter": ","}}

Coordinates (multiple files as tables)::

    {"database": "LAKE", "schema": "RAW",
     "tables": {"ORDERS": {"uri": "s3://.../orders.parquet", "format": "parquet"},
                "CUSTOMERS": {"uri": "/data/customers.ndjson", "format": "ndjson"}}}

Coordinates (scan a prefix — every table one level below becomes a dataset)::

    {"uri": "s3://bucket/lake/raw/", "scan": true}

With ``scan`` the ``uri`` is treated as a *prefix* (an S3 prefix or a local directory):
each file one level below becomes a table named after its stem, and each sub-folder one
level below becomes a table backed by *all* the data files beneath it (so a Spark/Hive-
style ``orders/part-*.parquet`` folder loads as one ``ORDERS`` table). The discovered set
is exposed through `list_tables`, so the batch compare can run over all N datasets.

S3 needs `boto3` (lazy import); Parquet needs `pyarrow` (lazy import). CSV/JSON/NDJSON use
only the stdlib.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from dvp.connectors.base import ColumnDef, Connector, HealthResult, QueryResult, TableRef
from dvp.core.errors import DVPError

_ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class FileConnector(Connector):
    dialect = "sqlite"  # backed by an embedded SQLite engine

    def __init__(
        self, coordinates: dict, auth_method: str = "none", secret: dict | None = None
    ) -> None:
        self.coordinates = coordinates or {}
        self.secret = secret or {}
        self._files: dict[str, str] = {}
        # table name -> the backing file URI(s), kept so freshness can read their arrival time.
        self._file_uris: dict[str, list[str]] = {}
        inline_tables: dict[str, dict] = {}
        for name, spec in self._table_specs().items():
            # A table may be a single file ("uri") or many files unioned ("uris" — a
            # scanned folder of part-files). Reading many concatenates their rows.
            uris = spec.get("uris") or [spec["uri"]]
            rows: list[dict] = []
            for uri in uris:
                rows.extend(_read_file(uri, spec.get("format"), spec.get("options") or {}, self.secret))
            inline_tables[name] = {"columns": _infer_columns(rows), "rows": rows}
            self._files[name] = uris[0] if len(uris) == 1 else f"{len(uris)} files @ {uris[0]}"
            self._file_uris[name] = list(uris)
        from dvp.connectors.local_sql import LocalSqlConnector

        self._inner = LocalSqlConnector(
            {
                "database": self.coordinates.get("database", ""),
                "schema": self.coordinates.get("schema", ""),
                "tables": inline_tables,
            }
        )

    def _table_specs(self) -> dict[str, dict]:
        c = self.coordinates
        if c.get("tables"):
            return {name.upper(): spec for name, spec in c["tables"].items()}
        uri = c.get("uri") or c.get("path")
        if not uri:
            raise DVPError("file connection needs a 'uri' (or a 'tables' map)")
        if c.get("scan"):
            # Prefix/directory scan — one table per file or sub-folder one level below.
            scanned = _scan_prefix(uri, self.secret)
            if not scanned:
                raise DVPError("scan found no data files under prefix", details={"uri": uri})
            return {
                name: {"uris": uris, "format": c.get("format"), "options": c.get("options")}
                for name, uris in scanned.items()
            }
        name = (c.get("table") or _stem(uri)).upper()
        return {name: {"uri": uri, "format": c.get("format"), "options": c.get("options")}}

    # ----- connector surface (delegates to the SQLite-backed inner connector) -----
    def test(self) -> HealthResult:
        return HealthResult(
            ok=True,
            detail=f"loaded {len(self._files)} file(s): {', '.join(self._files.values())[:200]}",
            category="ok",
        )

    def list_tables(self) -> list[str]:
        return self._inner.list_tables()

    def list_schemas(self) -> list[str]:
        return self._inner.list_schemas()

    def list_tables_in_schema(self, schema: str) -> list[dict]:
        return self._inner.list_tables_in_schema(schema)

    def sql_ref(self, table: TableRef) -> str:
        return self._inner.sql_ref(table)

    def get_schema(self, table: TableRef) -> list[ColumnDef]:
        return self._inner.get_schema(table)

    def count(self, table: TableRef, predicate: str | None = None) -> int:
        return self._inner.count(table, predicate)

    def metadata_row_count(self, table: TableRef) -> int | None:
        inner = getattr(self._inner, "metadata_row_count", None)
        return inner(table) if inner else None

    def execute(self, sql: str) -> QueryResult:
        return self._inner.execute(sql)

    def latest_watermark(self, table: TableRef, column: str) -> str | None:
        return self._inner.latest_watermark(table, column)

    def latest_file_arrival(self, table: TableRef) -> datetime | None:
        """When the newest backing file for this table last changed (UTC).

        This is the file-based notion of freshness: for S3 it is the newest object's
        ``LastModified``; for local files it is the newest ``mtime``. Returns ``None`` when
        no backing file can be stat-ed (e.g. an inline dataset with no file behind it)."""
        uris = self._file_uris.get(_table_name(table).upper())
        if not uris:
            return None
        stamps = [t for t in (_file_mtime(u, self.secret) for u in uris) if t is not None]
        return max(stamps) if stamps else None

    def spark_reader(self, spark: Any, table: TableRef, partition: dict | None = None) -> Any:
        raise NotImplementedError("Spark reads land with the cross-DB compare path")


# ----------------------------- prefix scan (one level below → N tables) -----------------------------
_DATA_EXTS = {"csv", "tsv", "json", "ndjson", "jsonl", "parquet", "pq"}


def _is_data_file(name: str) -> bool:
    """A readable data file — skip Spark/Hive markers (_SUCCESS, *.crc) and hidden files."""
    base = os.path.basename(name)
    if not base or base.startswith((".", "_")):
        return False
    ext = os.path.splitext(base)[1].lower().lstrip(".")
    return ext in _DATA_EXTS


def _scan_prefix(uri: str, secret: dict) -> dict[str, list[str]]:
    """Map each table one level below `uri` to its backing file URI(s).

    A file one level below → a single-file table (stem name); a sub-folder → a table
    (folder name) backed by every data file beneath it. Works for S3 and local dirs."""
    return _scan_s3(uri, secret) if uri.startswith("s3://") else _scan_local(uri)


def _scan_local(uri: str) -> dict[str, list[str]]:
    root = urlparse(uri).path if uri.startswith("file://") else uri
    if not os.path.isdir(root):
        raise DVPError("scan prefix is not a directory", details={"uri": uri})
    out: dict[str, list[str]] = {}
    for entry in sorted(os.listdir(root)):
        full = os.path.join(root, entry)
        if os.path.isfile(full):
            if _is_data_file(entry):
                out[_stem(entry).upper()] = [full]
        elif os.path.isdir(full):
            files = sorted(
                os.path.join(dp, f)
                for dp, _, fns in os.walk(full)
                for f in fns
                if _is_data_file(f)
            )
            if files:
                out[entry.upper()] = files
    return out


def _scan_s3(uri: str, secret: dict) -> dict[str, list[str]]:
    client = _s3_client(secret)
    p = urlparse(uri)
    bucket = p.netloc
    prefix = p.path.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    out: dict[str, list[str]] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key == prefix or key.endswith("/"):
                continue  # the prefix itself / a zero-byte folder marker
            if _is_data_file(key):
                out[_stem(key).upper()] = [f"s3://{bucket}/{key}"]
        for cp in page.get("CommonPrefixes", []):
            sub = cp["Prefix"]  # e.g. "lake/raw/orders/"
            name = sub[len(prefix) :].strip("/").upper()
            files = _list_s3_files(client, bucket, sub)
            if name and files:
                out[name] = files
    return out


def _list_s3_files(client, bucket: str, prefix: str) -> list[str]:  # noqa: ANN001
    """Every data file at or below `prefix` (recursive) as s3:// URIs."""
    uris: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith("/") and _is_data_file(key):
                uris.append(f"s3://{bucket}/{key}")
    return sorted(uris)


def _s3_client(secret: dict):  # noqa: ANN001
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("boto3 not installed. Install the 'aws' extra for S3 files.") from exc
    kwargs: dict = {}
    if secret.get("aws_access_key_id"):
        kwargs["aws_access_key_id"] = secret["aws_access_key_id"]
        kwargs["aws_secret_access_key"] = secret.get("aws_secret_access_key")
    if secret.get("region"):
        kwargs["region_name"] = secret["region"]
    return boto3.client("s3", **kwargs)


# ----------------------------- file arrival (freshness) -----------------------------
def _table_name(table: TableRef) -> str:
    """The bare table name, whether a TableRef or a plain string was passed."""
    return table.table if hasattr(table, "table") else str(table).split(".")[-1]


def _file_mtime(uri: str, secret: dict) -> datetime | None:
    """Last-modified time of a single file URI as a UTC datetime, or None if unavailable."""
    try:
        if uri.startswith("s3://"):
            p = urlparse(uri)
            head = _s3_client(secret).head_object(Bucket=p.netloc, Key=p.path.lstrip("/"))
            lm = head.get("LastModified")
            return lm.astimezone(UTC) if lm is not None else None
        path = urlparse(uri).path if uri.startswith("file://") else uri
        if not os.path.exists(path):
            return None
        return datetime.fromtimestamp(os.path.getmtime(path), tz=UTC)
    except Exception:
        return None


# ----------------------------- file reading -----------------------------
def _stem(uri: str) -> str:
    base = os.path.basename(urlparse(uri).path or uri)
    return os.path.splitext(base)[0] or "FILE"


def _infer_format(uri: str, fmt: str | None) -> str:
    if fmt:
        return fmt.lower()
    ext = os.path.splitext(urlparse(uri).path or uri)[1].lower().lstrip(".")
    return {
        "csv": "csv",
        "tsv": "csv",
        "json": "json",
        "ndjson": "ndjson",
        "jsonl": "ndjson",
        "parquet": "parquet",
        "pq": "parquet",
    }.get(ext, "csv")


def _load_bytes(uri: str, secret: dict) -> bytes:
    if uri.startswith("s3://"):
        return _load_s3(uri, secret)
    path = urlparse(uri).path if uri.startswith("file://") else uri
    if not os.path.exists(path):
        raise DVPError("file not found", details={"uri": uri})
    with open(path, "rb") as fh:
        return fh.read()


def _load_s3(uri: str, secret: dict) -> bytes:
    p = urlparse(uri)
    bucket, key = p.netloc, p.path.lstrip("/")
    client = _s3_client(secret)
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
        return obj["Body"].read()
    except Exception as exc:  # noqa: BLE001
        raise DVPError(f"S3 read failed for {uri}: {exc}") from exc


def _read_file(uri: str, fmt: str | None, options: dict, secret: dict) -> list[dict]:
    fmt = _infer_format(uri, fmt)
    if fmt == "parquet":
        return _read_parquet(_load_bytes(uri, secret))
    raw = _load_bytes(uri, secret).decode(options.get("encoding", "utf-8"))
    if fmt in ("csv", "tsv"):
        default_delim = "\t" if (fmt == "tsv" or uri.endswith(".tsv")) else ","
        delimiter = options.get("delimiter", default_delim)
        reader = csv.DictReader(io.StringIO(raw), delimiter=delimiter)
        return [_coerce_row(r) for r in reader]
    if fmt == "ndjson":
        return [_coerce_row(json.loads(ln)) for ln in raw.splitlines() if ln.strip()]
    if fmt == "json":
        data = json.loads(raw)
        if isinstance(data, dict):
            # allow {"records": [...]} or a single record
            data = data.get("records") or data.get("data") or [data]
        return [_coerce_row(r) for r in data]
    raise DVPError(f"unsupported file format: {fmt}")


def _read_parquet(data: bytes) -> list[dict]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pyarrow not installed. Install it to read Parquet files.") from exc
    table = pq.read_table(io.BytesIO(data))
    return table.to_pylist()


def _coerce_row(row: dict) -> dict:
    return {k: _coerce(v) for k, v in row.items()}


def _coerce(v: Any) -> Any:
    if v is None or isinstance(v, (int, float, bool)):
        return v
    s = str(v).strip()
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def _infer_columns(rows: list[dict]) -> list[dict]:
    names: list[str] = []
    seen: set[str] = set()
    for r in rows[:200]:
        for k in r:
            if k not in seen:
                seen.add(k)
                names.append(k)
    cols = []
    for name in names:
        vals = [r.get(name) for r in rows[:500] if r.get(name) is not None]
        cols.append(
            {
                "name": name,
                "data_type": _infer_type(vals),
                "nullable": any(r.get(name) is None for r in rows[:500]),
            }
        )
    return cols


def _infer_type(values: list) -> str:
    if not values:
        return "VARCHAR"
    if all(isinstance(v, bool) for v in values):
        return "BOOLEAN"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return "NUMBER"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        return "NUMBER"
    strs = [str(v) for v in values]
    if all(_ISO_TS.match(s) for s in strs):
        return "TIMESTAMP"
    if all(_ISO_DATE.match(s) for s in strs):
        return "DATE"
    return "VARCHAR"
