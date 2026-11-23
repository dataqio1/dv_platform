"""FileDatasetService — discover candidate file datasets in an S3/file connection and
track a schema-verified / blocked state (Figma screen 4).

Discovery mirrors CatalogService: build the connection's connector and call
``FileConnector.list_tables()`` to enumerate datasets (one per file / scanned sub-folder),
then ``get_schema()`` for each to get columns + a stable schema hash. The verified/blocked
state is REAL and persists: it is stored in the existing ``TableMetadata.config`` JSON
(``verification_status`` + ``schema_hash``), so no new table/column is needed. A later
schema fetch whose hash differs from the stored one is reported as ``outdated`` (drift).

Per-dataset schema-fetch failures become warnings, never exceptions (golden rule #3).
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from dvp.abstractions.secret_store import SecretStore
from dvp.connectors.base import TableRef
from dvp.core.errors import ValidationError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.models import Connection, TableMetadata
from dvp.db.session import MetadataStore
from dvp.services import audit

_FILE_SOURCE_TYPES = ("file", "s3")
VERIFICATION_STATES = ("verified", "blocked", "unverified", "outdated")


def schema_hash(columns: list[dict]) -> str:
    """Stable hash of a dataset's schema (name + type, order-independent) for drift detection."""
    norm = sorted(
        ((c.get("name") or "").lower(), (c.get("data_type") or "").upper()) for c in columns
    )
    payload = json.dumps(norm, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class FileDatasetService:
    def __init__(self, store: MetadataStore, secrets: SecretStore) -> None:
        self.store = store
        self.secrets = secrets

    # ----------------------------- list (READ) -----------------------------
    def list_datasets(
        self,
        ctx: TenantContext,
        connection_id: str,
        *,
        pattern: str | None = None,
        page: int = 1,
        size: int = 50,
        include_verified: bool = True,
        include_blocked: bool = True,
    ) -> dict:
        ctx.require(Permission.READ)
        connector, coords, source_type = self._connector(ctx, connection_id)
        if source_type not in _FILE_SOURCE_TYPES:
            raise ValidationError(
                "file-dataset discovery is only for file/s3 connections",
                details={"source_type": source_type},
            )

        database = coords.get("database") or ""
        schema_name = coords.get("schema") or ""
        try:
            names = connector.list_tables()
        except Exception as exc:  # noqa: BLE001 - surface as a single warning, don't 500
            return {
                "connection_id": connection_id,
                "datasets": [],
                "total": 0,
                "page": page,
                "size": size,
                "has_more": False,
                "warnings": [_warn("discovery_failed", str(exc))],
            }

        if pattern:
            names = [n for n in names if _match(n, pattern)]
        names = sorted(names)

        # Pre-load any stored verification metadata for these datasets (one query).
        stored = self._stored_metadata(ctx, [_ref(database, schema_name, n) for n in names])

        datasets: list[dict] = []
        warnings: list[dict] = []
        for name in names:
            ref = TableRef(database=database, schema=schema_name, table=name)
            ref_str = ref.fqn(quote="")
            info = self._describe(connector, ref, ref_str, stored.get(ref_str))
            if info.get("_warning"):
                warnings.append(info.pop("_warning"))
            else:
                info.pop("_warning", None)
            if info["verification_status"] == "verified" and not include_verified:
                continue
            if info["verification_status"] == "blocked" and not include_blocked:
                continue
            datasets.append(info)

        total = len(datasets)
        page = max(1, int(page or 1))
        size = max(1, min(int(size or 50), 500))
        start = (page - 1) * size
        window = datasets[start : start + size]
        return {
            "connection_id": connection_id,
            "datasets": window,
            "total": total,
            "page": page,
            "size": size,
            "has_more": start + size < total,
            "warnings": warnings,
        }

    # ----------------------------- verify (WRITE) -----------------------------
    def verify(self, ctx: TenantContext, connection_id: str, datasets: list[str]) -> dict:
        """Fetch + hash each dataset's schema and persist verification_status in
        TableMetadata.config. A dataset whose schema can't be read is recorded as ``blocked``
        with the reason; a readable one becomes ``verified``."""
        ctx.require(Permission.WRITE)
        if not datasets:
            raise ValidationError("no datasets to verify")
        connector, coords, source_type = self._connector(ctx, connection_id)
        if source_type not in _FILE_SOURCE_TYPES:
            raise ValidationError(
                "file-dataset verification is only for file/s3 connections",
                details={"source_type": source_type},
            )
        database = coords.get("database") or ""
        schema_name = coords.get("schema") or ""

        out: list[dict] = []
        warnings: list[dict] = []
        with self.store.session(ctx) as s:
            for raw in datasets:
                table = _dataset_name(raw)
                ref = TableRef(database=database, schema=schema_name, table=table)
                ref_str = ref.fqn(quote="")
                status, shash, blocked_reason, cols = self._fetch_status(connector, ref)
                if blocked_reason:
                    warnings.append(_warn("schema_fetch_failed", blocked_reason, {"table": table}))
                row = s.scalar(
                    select(TableMetadata).where(
                        TableMetadata.group_id == ctx.group_id,
                        TableMetadata.table_ref == ref_str,
                    )
                )
                if row is None:
                    row = TableMetadata(
                        org_id=ctx.org_id, group_id=ctx.group_id, table_ref=ref_str
                    )
                    s.add(row)
                cfg = dict(row.config or {})
                cfg["verification_status"] = status
                cfg["schema_hash"] = shash
                cfg["blocked_reason"] = blocked_reason
                row.config = cfg
                row.source = "file_verify"
                row.updated_by = ctx.user_id
                s.flush()
                out.append(
                    {
                        "table_ref": ref_str,
                        "verification_status": status,
                        "schema_hash": shash,
                        "blocked_reason": blocked_reason,
                        "columns_count": len(cols),
                    }
                )
            audit.record(
                s,
                ctx,
                "file_dataset.verify",
                entity_type="connection",
                entity_id=connection_id,
                detail={"count": len(out)},
            )
        return {"connection_id": connection_id, "verified": out, "warnings": warnings}

    # ----------------------------- helpers -----------------------------
    def _describe(
        self, connector, ref: TableRef, ref_str: str, stored: dict | None  # noqa: ANN001
    ) -> dict:
        """Build one dataset row: schema-derived fields + verification status (from stored
        metadata, re-checked for drift against the current schema hash)."""
        info: dict = {
            "uri": _uri_for(connector, ref),
            "table_name": ref.table,
            "format": _format_for(connector, ref),
            "columns_count": None,
            "schema_hash": None,
            "row_count": None,
            "verification_status": "unverified",
            "blocked_reason": None,
            "arrival_window": None,
            "last_modified": None,
            "_warning": None,
        }
        try:
            cols = connector.get_schema(ref)
            col_dicts = [
                {"name": c.name, "data_type": c.data_type, "nullable": c.nullable} for c in cols
            ]
            info["columns_count"] = len(col_dicts)
            info["schema_hash"] = schema_hash(col_dicts)
        except Exception as exc:  # noqa: BLE001 - surface per-dataset, don't 500
            info["verification_status"] = "blocked"
            info["blocked_reason"] = str(exc)
            info["_warning"] = _warn("schema_fetch_failed", str(exc), {"table": ref.table})
            return info

        # Cheap row count if the connector exposes a metadata estimate.
        info["row_count"] = _row_count(connector, ref)
        info["last_modified"] = _last_modified(connector, ref)

        if stored:
            prior = stored.get("verification_status")
            if prior == "verified":
                # Drift check: a changed schema hash downgrades verified → outdated.
                if stored.get("schema_hash") and stored["schema_hash"] != info["schema_hash"]:
                    info["verification_status"] = "outdated"
                else:
                    info["verification_status"] = "verified"
            elif prior == "blocked":
                info["verification_status"] = "blocked"
                info["blocked_reason"] = stored.get("blocked_reason")
            else:
                info["verification_status"] = prior or "unverified"
        return info

    def _fetch_status(self, connector, ref: TableRef):  # noqa: ANN001, ANN201
        try:
            cols = connector.get_schema(ref)
            col_dicts = [
                {"name": c.name, "data_type": c.data_type, "nullable": c.nullable} for c in cols
            ]
            return "verified", schema_hash(col_dicts), None, col_dicts
        except Exception as exc:  # noqa: BLE001
            return "blocked", None, str(exc), []

    def _stored_metadata(self, ctx: TenantContext, refs: list[str]) -> dict[str, dict]:
        if not refs:
            return {}
        with self.store.session(ctx) as s:
            rows = s.scalars(
                select(TableMetadata).where(
                    TableMetadata.group_id == ctx.group_id,
                    TableMetadata.table_ref.in_(refs),
                )
            ).all()
            return {r.table_ref: dict(r.config or {}) for r in rows}

    def _connector(self, ctx: TenantContext, connection_id: str):  # noqa: ANN201
        from dvp.connectors.registry import build_connector

        with self.store.session(ctx) as s:
            conn = s.get(Connection, connection_id)
            if conn is None or conn.group_id != ctx.group_id:
                raise ValidationError("connection not found", details={"id": connection_id})
            source_type = conn.source_type
            coords = dict(conn.coordinates)
            auth, secret_ref = conn.auth_method, conn.secret_ref
        secret = self.secrets.get(ctx, secret_ref) if (secret_ref and self.secrets) else None
        return build_connector(source_type, coords, auth, secret), coords, source_type


# ----------------------------- module helpers -----------------------------
def _ref(database: str, schema: str, table: str) -> str:
    return TableRef(database=database, schema=schema, table=table).fqn(quote="")


def _dataset_name(raw: str) -> str:
    """Accept either a bare dataset name or a FQN/URI; take the last meaningful segment."""
    s = (raw or "").strip()
    if not s:
        raise ValidationError("dataset reference is empty")
    if "://" in s:  # a URI like s3://bucket/path/orders.csv
        stem = s.rstrip("/").rsplit("/", 1)[-1]
        return stem.rsplit(".", 1)[0].upper()
    return s.split(".")[-1].upper()


def _match(name: str, pattern: str) -> bool:
    """Prefix/substring/glob filter (case-insensitive). '*'/'?' → glob, else substring."""
    import fnmatch

    n, p = name.lower(), pattern.lower()
    if any(ch in p for ch in "*?[]"):
        return fnmatch.fnmatch(n, p)
    return p in n


def _uri_for(connector, ref: TableRef) -> str | None:  # noqa: ANN001
    files = getattr(connector, "_files", None)
    if isinstance(files, dict):
        return files.get(ref.table.upper())
    return None


def _format_for(connector, ref: TableRef) -> str | None:  # noqa: ANN001
    uris = getattr(connector, "_file_uris", None)
    if isinstance(uris, dict):
        lst = uris.get(ref.table.upper())
        if lst:
            from dvp.connectors.file_source import _infer_format

            return _infer_format(lst[0], None)
    return None


def _row_count(connector, ref: TableRef) -> int | None:  # noqa: ANN001
    for attr in ("metadata_row_count", "count"):
        fn = getattr(connector, attr, None)
        if fn is None:
            continue
        try:
            val = fn(ref)
            if val is not None:
                return int(val)
        except Exception:  # noqa: BLE001 - row count is best-effort
            continue
    return None


def _last_modified(connector, ref: TableRef) -> str | None:  # noqa: ANN001
    fn = getattr(connector, "latest_file_arrival", None)
    if fn is None:
        return None
    try:
        ts = fn(ref)
        return ts.isoformat() if ts is not None else None
    except Exception:  # noqa: BLE001
        return None


def _warn(code: str, message: str, details: dict | None = None) -> dict:
    return {"code": code, "message": message, "details": details}
