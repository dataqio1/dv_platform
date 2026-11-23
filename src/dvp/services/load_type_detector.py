"""LoadTypeDetectorService — classify HOW a monitored table is loaded and suggest the
column mappings the UI's "detect & accept" flow (Figma screen 12) needs.

This is the detect half of the load-type flow: the existing
``POST /monitors/{id}:load-type`` only *sets* a value; this service inspects each table in
the monitor's scope and proposes one of five UI load types — ``scd1 | scd2 | append |
full_reload | other`` — with a confidence, a human reason, plausible alternatives, and
per-role suggested columns (watermark/LOADED_AT, BATCH_ID, BUSINESS_KEY, VALID_FROM,
IS_CURRENT, INGESTED_AT, EVENT_ID).

It reuses the real engines rather than reinventing them (golden rule: no parallel logic):
  * ``services.table_metadata.classify_pattern`` — the 6-pattern auto-classifier — is the
    primary signal, mapped onto the 5 UI types.
  * ``observability.watermark.detect_watermark`` — ranks the best load/update timestamp,
    which becomes the watermark/LOADED_AT role suggestion.

Flow mirrors WatermarkService: load the connection, build its connector, fetch each
scoped table's schema (per-table fetch failures become warnings, never swallowed —
golden rule #3), then run the pure classification.
"""

from __future__ import annotations

from sqlalchemy import select

from dvp.abstractions.secret_store import SecretStore
from dvp.connectors.base import TableRef
from dvp.core.errors import NotFoundError, ValidationError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.models import DetectionRun, Monitor
from dvp.db.session import MetadataStore
from dvp.observability.watermark import ColumnInfo, detect_watermark
from dvp.services.table_metadata import classify_pattern, resolve_conventions

# The six classifier patterns map onto the five UI load types (Figma screen 12).
#   scd2            -> scd2        (versioned history: effective_from + current flag)
#   cdc             -> scd1        (in-place inserts+updates = overwrite current row = SCD-1)
#   append          -> append
#   full_snapshot   -> full_reload (truncate & load)
#   partitioned     -> append      (partitions accrete new rows)
#   reference       -> full_reload (small lookup, typically replaced)
#   unknown         -> other
_PATTERN_TO_LOAD_TYPE = {
    "scd2": "scd2",
    "cdc": "scd1",
    "append": "append",
    "full_snapshot": "full_reload",
    "partitioned": "append",
    "reference": "full_reload",
    "unknown": "other",
}

LOAD_TYPES = ("scd1", "scd2", "append", "full_reload", "other")

# Per-role column-name candidate patterns. Reuses/extends the name sets the classifier
# already keys on so detection stays consistent with classify_pattern. A column matches a
# role when its lowercased name equals, ends with, or contains one of the role's patterns.
_ROLE_PATTERNS: dict[str, tuple[str, ...]] = {
    # watermark/LOADED_AT is filled from detect_watermark; these are the fallback names.
    "watermark": (
        "loaded_at", "load_ts", "load_timestamp", "load_date", "etl_loaded_at",
        "dw_loaded_at", "_loaded_at", "dbt_loaded_at", "updated_at", "modified_at",
        "last_modified", "last_updated",
    ),
    "batch_id": ("batch_id", "batch_no", "batch_number", "load_id", "run_id", "job_id"),
    "business_key": (
        "id", "key", "pk", "business_key", "natural_key", "code", "number",
    ),
    "valid_from": (
        "valid_from", "effective_from", "eff_from", "start_date", "valid_start",
        "effective_date",
    ),
    "valid_to": ("valid_to", "effective_to", "eff_to", "end_date", "valid_end"),
    "is_current": (
        "is_current", "current_flag", "is_active", "active_flag", "iscurrent",
        "current_ind",
    ),
    "ingested_at": (
        "ingested_at", "ingestion_time", "inserted_at", "insert_ts", "created_at",
        "created_ts", "_ingested_at",
    ),
    "event_id": ("event_id", "event_key", "evt_id", "message_id", "offset", "sequence"),
}


class LoadTypeDetectorService:
    def __init__(self, store: MetadataStore, secrets: SecretStore) -> None:
        self.store = store
        self.secrets = secrets

    def detect(self, ctx: TenantContext, monitor_id: str) -> dict:
        """Classify every table in the monitor's scope + suggest column mappings by role.

        Convention-first & multi-convention: the monitor's attached ETL conventions (named ones
        inherited from the connection, plus any inline one) seed the classifier. When several are
        attached (e.g. IDMC-CDC + S3-Loader), each table resolves to the **best-covering** one and
        reports which matched + its per-role coverage (advisory — absent columns flagged, never
        forced; §3.4–3.5)."""
        ctx.require(Permission.READ)
        connection_id, scope, conventions = self._monitor_scope(ctx, monitor_id)
        schemas, warnings = self._load_schemas(ctx, connection_id, scope)

        # Watermark role is derived once across all tables (monitor-level, like the UI).
        wm_suggestions = detect_watermark(schemas)
        watermark = wm_suggestions[0].as_dict() if wm_suggestions else None

        results: list[dict] = []
        for table, cols in schemas.items():
            results.append(_classify_table(table, cols, watermark, conventions))

        out = {
            "monitor_id": monitor_id,
            "connection_id": connection_id,
            "tables_inspected": list(schemas.keys()),
            "watermark": watermark,
            "conventions": [{"id": c.get("id"), "name": c.get("name")} for c in conventions] or None,
            "results": results,
            "warnings": warnings,
        }
        # Surface partial coverage of the MATCHED convention plainly (golden rule #3).
        if conventions:
            for r in results:
                missing = r.get("coverage", {}).get("missing_roles") or []
                matched = r.get("matched_convention")
                if matched and missing:
                    warnings.append({
                        "code": "metadata_columns_missing",
                        "message": f"table '{r['table_ref']}' (convention {matched['name']}) is "
                        f"missing role(s): {', '.join(missing)} — auto-detected for those",
                        "details": {"table": r["table_ref"], "convention": matched,
                                    "missing_roles": missing},
                    })
        return out

    # ----- async (progressive bulk detection) -----
    def start(
        self, ctx: TenantContext, *, connection_id: str, scope: dict, monitor_id: str | None = None
    ) -> dict:
        """Create a detection run (status=running) for a connection + scope — works BEFORE a monitor
        exists (the create flow). The caller submits `run_detection` to the worker pool; the UI
        polls `get` for streaming results + progress."""
        ctx.require(Permission.WRITE)
        total = len(scope.get("tables") or [])
        with self.store.session(ctx) as s:
            run = DetectionRun(
                org_id=ctx.org_id, group_id=ctx.group_id, monitor_id=monitor_id,
                connection_id=connection_id, scope=dict(scope or {}), status="running",
                progress={"pct": 0, "processed": 0, "total": total}, results=[], warnings=[],
            )
            s.add(run)
            s.flush()
            return _run_dict(run)

    def start_for_monitor(self, ctx: TenantContext, monitor_id: str) -> dict:
        """Convenience: start a detection run from an existing monitor (reads its connection+scope)."""
        connection_id, scope, _ = self._monitor_scope(ctx, monitor_id)
        return self.start(ctx, connection_id=connection_id, scope=scope, monitor_id=monitor_id)

    def get(self, ctx: TenantContext, run_id: str) -> dict:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            run = s.scalar(
                select(DetectionRun).where(
                    DetectionRun.id == run_id, DetectionRun.group_id == ctx.group_id
                )
            )
            if run is None:
                raise NotFoundError("detection run not found", details={"id": run_id})
            return _run_dict(run)

    def run_detection(self, ctx: TenantContext, run_id: str) -> None:
        """Process a detection run: batch-fetch all schemas, classify each table, and STREAM the
        per-table result + progress after each (so a 100-table run fills the UI table as it goes).
        Best-effort — any failure marks the run errored rather than crashing a worker thread."""
        try:
            with self.store.session(ctx) as s:
                run = s.get(DetectionRun, run_id)
                if run is None or run.group_id != ctx.group_id:
                    return
                monitor_id = run.monitor_id
                connection_id = run.connection_id
                scope = dict(run.scope or {})
            conventions = self._conventions_for(ctx, connection_id, monitor_id)
            schemas, warnings = self._load_schemas(ctx, connection_id, scope)
            wm = detect_watermark(schemas)
            watermark = wm[0].as_dict() if wm else None
            tables = list(schemas.keys())
            total = len(tables)
            results: list[dict] = []
            for i, table in enumerate(tables, start=1):
                results.append(_classify_table(table, schemas[table], watermark, conventions))
                with self.store.session(ctx) as s:  # commit per table → pollers see partial results
                    run = s.get(DetectionRun, run_id)
                    if run is None:
                        return
                    run.results = list(results)
                    run.progress = {"pct": round(i / total * 100) if total else 100,
                                    "processed": i, "total": total}
            with self.store.session(ctx) as s:
                run = s.get(DetectionRun, run_id)
                if run is not None:
                    run.status = "done"
                    run.warnings = warnings
                    run.progress = {"pct": 100, "processed": total, "total": total}
        except Exception as exc:  # noqa: BLE001 - mark errored, never crash the worker
            with self.store.session(ctx) as s:
                run = s.get(DetectionRun, run_id)
                if run is not None:
                    run.status = "error"
                    run.error = str(exc)

    # ----- helpers -----
    def _monitor_scope(
        self, ctx: TenantContext, monitor_id: str
    ) -> tuple[str, dict, list[dict]]:
        """Resolve the monitor's attached conventions (named ids → dicts, plus any inline one) into
        an ordered list the classifier resolves per-table."""
        from dvp.services.metadata_conventions import MetadataConventionService

        with self.store.session(ctx) as s:
            m = s.get(Monitor, monitor_id)
            if m is None or m.group_id != ctx.group_id or m.status == "deleted":
                raise NotFoundError("monitor not found", details={"id": monitor_id})
            connection_id = m.connection_id
            scope = dict(m.scope or {})
            conv_ids = list(m.metadata_convention_ids or [])
            inline_roles = dict((m.metadata_columns or {}).get("roles") or {})
        conventions = MetadataConventionService(self.store).resolve_ids(ctx, conv_ids)
        if inline_roles:
            conventions = [*conventions, {"id": None, "name": "(inline)", "roles": inline_roles}]
        return connection_id, scope, conventions

    def _conventions_for(
        self, ctx: TenantContext, connection_id: str | None, monitor_id: str | None
    ) -> list[dict]:
        """Attached ETL conventions for a detection run — from the monitor if set, else the
        connection (both carry named ids + an optional inline convention)."""
        from dvp.db.models import Connection
        from dvp.services.metadata_conventions import MetadataConventionService

        with self.store.session(ctx) as s:
            conv_ids: list[str] = []
            inline_roles: dict = {}
            if monitor_id:
                m = s.get(Monitor, monitor_id)
                if m is not None and m.group_id == ctx.group_id:
                    conv_ids = list(m.metadata_convention_ids or [])
                    inline_roles = dict((m.metadata_columns or {}).get("roles") or {})
            if not conv_ids and not inline_roles and connection_id:
                conn = s.get(Connection, connection_id)
                if conn is not None and conn.group_id == ctx.group_id:
                    conv_ids = list(conn.metadata_convention_ids or [])
                    inline_roles = dict((conn.metadata_columns or {}).get("roles") or {})
        conventions = MetadataConventionService(self.store).resolve_ids(ctx, conv_ids)
        if inline_roles:
            conventions = [*conventions, {"id": None, "name": "(inline)", "roles": inline_roles}]
        return conventions

    def _load_schemas(
        self, ctx: TenantContext, connection_id: str, scope: dict
    ) -> tuple[dict[str, list[ColumnInfo]], list[dict]]:
        from dvp.connectors.registry import build_connector
        from dvp.db.models import Connection

        with self.store.session(ctx) as s:
            conn = s.get(Connection, connection_id)
            if conn is None or conn.group_id != ctx.group_id:
                raise ValidationError(
                    "connection not found", details={"connection_id": connection_id}
                )
            source_type = conn.source_type
            coordinates = dict(conn.coordinates)
            auth_method = conn.auth_method
            secret_ref = conn.secret_ref

        secret = self.secrets.get(ctx, secret_ref) if (secret_ref and self.secrets) else None
        connector = build_connector(source_type, coordinates, auth_method, secret)

        database = scope.get("database") or coordinates.get("database") or ""
        schema_name = scope.get("schema") or coordinates.get("schema") or ""
        tables = scope.get("tables") or []
        if not tables and hasattr(connector, "list_tables"):
            tables = connector.list_tables()
        if not tables:
            raise ValidationError(
                "monitor scope lists no tables to classify (scope.tables)",
                details={"scope": scope},
            )

        schemas: dict[str, list[ColumnInfo]] = {}
        warnings: list[dict] = []

        # Fast path: pull EVERY table's columns in ONE query for the schema (the slow part of bulk
        # detection is 100+ per-table round-trips, not the data volume — schema is metadata). Falls
        # back to per-table get_schema when the connector can't batch.
        batch: dict | None = None
        if hasattr(connector, "columns_for_schema"):
            try:
                batch = connector.columns_for_schema(schema_name, database)
            except Exception as exc:  # noqa: BLE001 - fall back per-table, note it
                warnings.append({"code": "batch_schema_failed",
                                 "message": f"batched schema fetch failed, reading per-table: {exc}",
                                 "details": {"schema": schema_name}})

        want = {t.upper(): t for t in tables}
        if batch is not None:
            for name, cols in batch.items():
                orig = want.get(name.upper())
                if orig is None:
                    continue  # a table not in scope
                schemas[orig] = [ColumnInfo(name=c.name, data_type=c.data_type, nullable=c.nullable)
                                 for c in cols]
            missing = [t for t in tables if t not in schemas]
            tables = missing  # only per-table-fetch what the batch didn't cover

        for table in tables:
            ref = TableRef(database=database, schema=schema_name, table=table)
            try:
                cols = connector.get_schema(ref)
                schemas[table] = [
                    ColumnInfo(name=c.name, data_type=c.data_type, nullable=c.nullable)
                    for c in cols
                ]
            except Exception as exc:  # noqa: BLE001 - surface, don't swallow (rule #3)
                warnings.append(
                    {
                        "code": "schema_fetch_failed",
                        "message": f"could not read schema for table '{table}': {exc}",
                        "details": {"table": table},
                    }
                )
        return schemas, warnings


def _run_dict(r: DetectionRun) -> dict:
    return {
        "id": r.id,
        "monitor_id": r.monitor_id,
        "connection_id": r.connection_id,
        "scope": r.scope or {},
        "status": r.status,
        "progress": r.progress or {"pct": 0, "processed": 0, "total": 0},
        "results": r.results or [],
        "warnings": r.warnings or [],
        "error": r.error,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


# ----------------------------- pure classification -----------------------------
def _classify_table(
    table: str, cols: list[ColumnInfo], watermark: dict | None, conventions: list[dict] | None = None
) -> dict:
    """Classify one table into a UI load type + suggest per-role columns. When conventions are
    attached, pick the best-covering one for THIS table and seed the classifier with its roles."""
    col_dicts = [{"name": c.name, "data_type": c.data_type, "nullable": c.nullable} for c in cols]
    resolved = resolve_conventions(conventions or [], col_dicts)
    effective_roles = resolved["roles"] or None
    suggestion = classify_pattern(col_dicts, convention=effective_roles)
    pattern = suggestion.get("load_pattern", "unknown")
    load_type = _PATTERN_TO_LOAD_TYPE.get(pattern, "other")
    confidence = float(suggestion.get("confidence", 0.5))
    reason = _reason(load_type, pattern, suggestion)

    candidates = _role_candidates(cols, watermark)
    mappings = {role: (cands[0] if cands else None) for role, cands in candidates.items()}

    alternatives = _alternatives(load_type, pattern, candidates, confidence)

    out = {
        "table_ref": table,
        "load_type": load_type,
        "load_pattern": pattern,
        "confidence": round(confidence, 3),
        "reason": reason,
        # The resolved SCD2 role columns + active-definition knobs from the classifier, so the UI
        # can show/store them directly.
        "suggested_metadata": {
            k: v for k, v in suggestion.items() if k not in ("load_pattern", "confidence")
        },
        "column_mappings": mappings,
        "column_candidates": candidates,
        "alternatives": alternatives,
    }
    if conventions:
        # Which attached convention won for this table (coverage-based) + its per-role coverage, so
        # the UI can show "resolved via IDMC-CDC" and let the user override.
        out["matched_convention"] = resolved["matched"]
        out["coverage"] = {
            "coverage": resolved["coverage"],
            "missing_roles": [r for r, ok in resolved["coverage"].items() if not ok],
            "present_roles": [r for r, ok in resolved["coverage"].items() if ok],
        }
        out["convention_candidates"] = resolved["candidates"]
    return out


_REASONS = {
    "scd2": "versioned history: an effective-from date and a current-row flag were found",
    "scd1": "a change timestamp indicates rows are inserted and updated in place (overwrite)",
    "append": "an append-only load signal (load/ingest timestamp) was found",
    "full_reload": "no incremental signal — the table looks truncated & reloaded each run",
    "other": "no known load pattern matched the columns",
}


def _reason(load_type: str, pattern: str, suggestion: dict) -> str:
    base = _REASONS.get(load_type, "classified from the column profile")
    keyed = [v for k, v in suggestion.items() if k.endswith("_column") and v]
    if keyed:
        return f"{base} (keys on {', '.join(keyed)})"
    return base


def _role_candidates(
    cols: list[ColumnInfo], watermark: dict | None
) -> dict[str, list[str]]:
    """Per-role candidate columns by name match. The watermark/LOADED_AT role is seeded with
    the cross-table watermark suggestion (if present in this table) first."""
    names = [c.name for c in cols]
    lower = {c.name: c.name.lower() for c in cols}
    out: dict[str, list[str]] = {}
    for role, patterns in _ROLE_PATTERNS.items():
        matched = [
            n for n in names
            if any(lower[n] == p or lower[n].endswith(p) or p in lower[n] for p in patterns)
        ]
        out[role] = matched
    # Promote the detected watermark column to the front of the watermark role when the
    # table actually contains it (keeps the detect-and-accept default consistent).
    if watermark and watermark.get("column"):
        wcol = watermark["column"]
        match = next((n for n in names if n.lower() == wcol.lower()), None)
        if match is not None:
            out["watermark"] = [match] + [c for c in out["watermark"] if c != match]
    return out


def _alternatives(
    load_type: str, pattern: str, candidates: dict[str, list[str]], confidence: float
) -> list[dict]:
    """Other plausible UI load types for this table, with a short why, best-first."""
    alts: list[dict] = []
    seen = {load_type}

    def add(lt: str, why: str) -> None:
        if lt not in seen:
            seen.add(lt)
            alts.append({"load_type": lt, "reason": why})

    # SCD-2 structural signals present but not chosen → offer it.
    if candidates.get("valid_from") and candidates.get("is_current"):
        add("scd2", "has effective-from + current-flag columns")
    # A business key + watermark looks like SCD-1/CDC overwrite.
    if candidates.get("business_key") and candidates.get("watermark"):
        add("scd1", "has a key + an update timestamp (overwrite-in-place)")
    # A load/ingest timestamp without updates looks append-only.
    if candidates.get("ingested_at") or candidates.get("watermark"):
        add("append", "has a load/ingest timestamp")
    # Full reload is always a fallback the user can pick.
    add("full_reload", "no incremental signal — table may be truncated & reloaded")
    add("other", "none of the known patterns fit")
    return alts
