"""Per-table load-pattern catalog + injection API.

`load_pattern` drives every observability pillar (the doc's section 2 — the #1 cause of
false positives when wrong). It can be:
  * **injected** from an external system (a customer's custom ingestion software) via
    bulk upsert keyed by table_ref, with provenance in `source`;
  * **auto-classified** from a column profile as a fallback (user/ingestion confirmable);
  * **set in the UI**.

Resolution: a per-table record overrides the monitor-level default (`monitor.load_type`).
"""

from __future__ import annotations

from sqlalchemy import select

from dvp.core.errors import NotFoundError, ValidationError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.models import TableMetadata
from dvp.db.session import MetadataStore
from dvp.services import audit

# The six production load patterns (doc §2) + "unknown".
LOAD_PATTERNS = ("append", "cdc", "full_snapshot", "scd2", "partitioned", "reference", "unknown")

# Back-compat: the legacy monitor-level load_type maps onto the richer taxonomy.
_LEGACY_MAP = {"incremental": "cdc", "full_reload": "full_snapshot", "append": "append"}

# SCD2 "active definition" knobs live in config.scd2 (tuning, not a schema column). When these
# arrive as flat fields (from the data-driven UI / injection) they are nested under config.scd2.
_SCD2_CONFIG_KEYS = ("active_mode", "high_date_value", "open_end_is_null", "current_flag_value")
_DEFAULT_HIGH_DATE = "9999-12-31"

# ETL metadata-column roles, tool-agnostic. Every pipeline stamps some subset into every table;
# names differ, roles don't. Declared once per connection/monitor and projected onto each table.
METADATA_ROLES = (
    "loaded_at", "updated_at", "created_at", "batch_id", "source_system", "job_run_id",
    "soft_delete_flag", "record_hash", "effective_from", "effective_to", "current_flag",
)

# Shipped presets: tool → role→column map (+ optional scd2 config). `custom`/`auto` are handled in
# the UI/detector. Extensible: add a tool here and the API + first-step picker pick it up.
METADATA_PRESETS: dict[str, dict] = {
    "dbt_snapshot": {
        "label": "dbt snapshot",
        "roles": {
            "effective_from": "dbt_valid_from", "effective_to": "dbt_valid_to",
            "updated_at": "dbt_updated_at", "record_hash": "dbt_scd_id",
        },
        "scd2": {"active_mode": "end_date", "open_end_is_null": True},
    },
    "fivetran": {
        "label": "Fivetran",
        "roles": {"loaded_at": "_fivetran_synced", "soft_delete_flag": "_fivetran_deleted"},
    },
    "airbyte": {
        "label": "Airbyte",
        "roles": {"loaded_at": "_airbyte_extracted_at", "job_run_id": "_airbyte_raw_id"},
    },
    "stitch": {
        "label": "Stitch",
        "roles": {
            "loaded_at": "_sdc_received_at", "batch_id": "_sdc_batched_at",
            "soft_delete_flag": "_sdc_deleted_at", "source_system": "_sdc_source",
        },
    },
    "informatica_dw": {
        "label": "Informatica / custom DW",
        "roles": {
            "loaded_at": "dw_load_ts", "updated_at": "dw_update_ts",
            "batch_id": "dw_batch_id", "source_system": "src_system",
        },
    },
    "custom": {"label": "Custom (declare your own)", "roles": {}},
}

# Which TableMetadata role column a metadata-role projects onto, when present in a table.
_ROLE_TO_FIELD: dict[str, str] = {
    "loaded_at": "watermark_column",
    "updated_at": "changed_at_column",
    "created_at": "watermark_column",
    "batch_id": "batch_id_column",
    "effective_from": "effective_from_column",
    "effective_to": "effective_to_column",
    "current_flag": "current_flag_column",
}


def metadata_presets() -> dict:
    """The ETL metadata-column preset library + the role catalog, for the first-step picker."""
    return {"roles": list(METADATA_ROLES), "presets": METADATA_PRESETS}


def _sql_lit(v: object) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def scd2_active_predicate(meta: dict) -> str | None:
    """Build the SQL WHERE fragment that selects a table's *active* (open) SCD2 rows, from its
    resolved metadata. Returns None when the metadata can't express 'active'. Pure — the predicate
    is reused by the active-count metric and the integrity checks so 'active' means one thing.

    A `soft_delete_flag` role (in config) is AND-ed in — active rows are never deleted.
    """
    cfg = (meta.get("config") or {}).get("scd2", {}) or {}
    mode = cfg.get("active_mode")
    flag_col = meta.get("current_flag_column")
    end_col = meta.get("effective_to_column")
    # Infer the mode if not set explicitly.
    if not mode:
        mode = "flag" if flag_col else ("end_date" if end_col else None)
    pred: str | None = None
    if mode == "flag" and flag_col:
        val = cfg.get("current_flag_value", "Y")
        pred = f'"{flag_col}" = {_sql_lit(val)}'
    elif mode == "end_date" and end_col:
        high = _sql_lit(cfg.get("high_date_value", _DEFAULT_HIGH_DATE))
        parts = [f'"{end_col}" >= {high}']
        if cfg.get("open_end_is_null", True):
            parts.append(f'"{end_col}" IS NULL')
        pred = "(" + " OR ".join(parts) + ")"
    if pred is None:
        return None
    soft = (meta.get("config") or {}).get("soft_delete_flag") or cfg.get("soft_delete_flag")
    if soft:
        pred = f'{pred} AND ("{soft}" IS NULL OR "{soft}" = 0)'
    return pred


def project_convention(convention: dict | None, columns: list[dict]) -> dict:
    """Project an ETL metadata-column convention (role→column) onto ONE table's columns.

    Advisory, not assumed (docs/design/11 §3.5): a role is applied only when its column actually
    exists in the table. Returns the per-table `TableMetadata` field values for present roles, a
    coverage map, and the present/missing role lists so the UI/warnings can report partial coverage.
    """
    present = {c.get("name", "") for c in columns}
    conv = convention or {}
    coverage = {role: (col in present) for role, col in conv.items()}
    fields: dict = {}
    for role, col in conv.items():
        if col in present and role in _ROLE_TO_FIELD:
            fields[_ROLE_TO_FIELD[role]] = col
    soft = conv.get("soft_delete_flag")
    if soft and soft in present:
        fields["config"] = {"soft_delete_flag": soft}
    return {
        "fields": fields,
        "coverage": coverage,
        "present_roles": sorted(r for r, ok in coverage.items() if ok),
        "missing_roles": sorted(r for r, ok in coverage.items() if not ok),
    }


def resolve_conventions(conventions: list[dict], columns: list[dict]) -> dict:
    """Pick the best-covering ETL convention for ONE table (docs/design/11 §3.4 — the two-tool
    case). `conventions` is an ordered list of ``{id?, name?, roles}``; each table resolves to the
    convention whose declared columns actually exist in it, highest coverage wins, ties → attachment
    order, none → no match (auto-detect downstream). Pure.

    Returns the winning convention's projected `TableMetadata` fields + which one matched + each
    convention's present-role list (so the UI can explain the pick / offer an override).
    """
    scored = []
    for conv in conventions or []:
        proj = project_convention(conv.get("roles") or {}, columns)
        scored.append((len(proj["present_roles"]), conv, proj))
    best = None
    for present, conv, proj in scored:  # stable: only replace on strictly greater coverage
        if present > 0 and (best is None or present > best[0]):
            best = (present, conv, proj)
    matched = None if best is None else {"id": best[1].get("id"), "name": best[1].get("name")}
    return {
        "matched": matched,
        "fields": {} if best is None else best[2]["fields"],
        "coverage": {} if best is None else best[2]["coverage"],
        "roles": {} if best is None else (best[1].get("roles") or {}),
        "candidates": [
            {"id": c.get("id"), "name": c.get("name"), "present_roles": p["present_roles"]}
            for _, c, p in scored
        ],
    }


def build_scd2_rule_pack(table_ref: str, meta: dict) -> dict:
    """The load-aware DQ rule pack for an SCD2 table, from its resolved metadata (docs/design/11
    §6.3). Pure — returns rule *specs*; the service materializes them with origin='system'. Only
    emits a rule when the columns it needs are present (partial coverage ⇒ a partial pack).

    Returns {"dq_rules": [spec, …], "metrics": [metric_spec, …]} where a dq_rule spec is ready for
    RuleService.create and a metric spec feeds the active-count baseline (metrics engine).
    """
    pred = scd2_active_predicate(meta)
    key = list(meta.get("business_key") or [])
    eff_from = meta.get("effective_from_column")
    eff_to = meta.get("effective_to_column")
    table = table_ref.split(".")[-1]
    dq_rules: list[dict] = []

    def spec(name, rtype, columns, config, severity, state, confidence, description, scope="full"):
        return {"name": name, "type": rtype, "columns": columns, "config": config,
                "severity": severity, "state": state, "origin": "system",
                "confidence": confidence, "scope": scope, "table_ref": table_ref,
                "description": description}

    # 2. One active version per business key — a correctness invariant (fail).
    if pred and key:
        dq_rules.append(spec(
            f"scd2_active_unique__{table}", "scd2_active_unique", key,
            {"key_columns": key, "active_sql": pred}, "fail", "enabled", 0.9,
            "At most one active (open) version is allowed per business key.",
        ))
    # 4. No-overlap / gap closure — heaviest, so suggested (off until the user enables).
    if key and eff_from and eff_to:
        dq_rules.append(spec(
            f"scd2_no_overlap__{table}", "scd2_no_overlap", key,
            {"key_columns": key, "effective_from_column": eff_from, "effective_to_column": eff_to},
            "warning", "suggested", 0.7,
            "Version [from, to) intervals must not overlap for a business key.",
        ))
    # 3. Freshness from effective-from — 'did the load run?' for a never-truncated table.
    if eff_from:
        dq_rules.append(spec(
            f"freshness__{table}", "freshness", [eff_from],
            {"max_age_hours": 24, "source": "effective_from"}, "warning", "enabled", 0.6,
            "Newest effective-from date must be recent.", scope="incremental",
        ))

    # 1. Active-record count baseline — a metric (history/charts), not a point-in-time rule.
    metrics: list[dict] = []
    if pred:
        metrics.append({
            "name": f"{table} active rows",
            "query": f"SELECT COUNT(*) AS v FROM {table_ref} WHERE {pred}",
            "description": f"Count of active (open) SCD2 rows for {table_ref}.",
            "rule": {"type": "deviation", "severity": "warn",
                     "config": {"aggregate": "avg", "window": {"observations": 30},
                                "direction": "both", "tolerance_pct": 10}},
        })
    return {"dq_rules": dq_rules, "metrics": metrics}

# Columns that are writable on a metadata record (what an external system can inject).
_FIELDS = (
    "load_pattern",
    "watermark_column",
    "watermark_type",
    "partition_column",
    "effective_from_column",
    "effective_to_column",
    "current_flag_column",
    "changed_at_column",
    "batch_id_column",
    "business_key",
    "freshness_sla_seconds",
    "config",
)


def default_pattern_for(load_type: str | None) -> str:
    return _LEGACY_MAP.get(load_type or "", "unknown")


# UI-facing field descriptors referenced by the pattern specs below. Keeping the catalog
# here (next to LOAD_PATTERNS) makes "support a new load pattern later" a single-file change
# that the creation UI adapts to automatically — no frontend edit needed to add a pattern.
_FIELD_SPECS: dict[str, dict] = {
    "watermark_column": {
        "label": "Watermark column",
        "input": "column",
        "help": "Monotonic load/update column — incremental runs read only rows past its high-water-mark.",
    },
    "watermark_type": {
        "label": "Watermark type",
        "input": "enum",
        "options": ["timestamp", "date", "numeric"],
    },
    "changed_at_column": {"label": "Changed-at column", "input": "column"},
    "effective_from_column": {
        "label": "Effective-from column",
        "input": "column",
        "help": "When a version became effective (e.g. src_eff_start_date, dbt_valid_from).",
    },
    "effective_to_column": {
        "label": "Effective-to (end) column",
        "input": "column",
        "help": "When a version stopped being effective. A max-future/NULL value means the row is "
        "still active (e.g. src_eff_end_date, dbt_valid_to).",
    },
    "current_flag_column": {
        "label": "Current-flag column",
        "input": "column",
        "help": "Boolean/flag marking the active version (e.g. is_current).",
    },
    "batch_id_column": {
        "label": "Batch id column",
        "input": "column",
        "help": "The ETL batch that wrote the row (e.g. batch_id, dw_batch_id). Used for "
        "“did the load run” freshness.",
    },
    "business_key": {
        "label": "Business key column(s)",
        "input": "columns",
        "help": "The natural key the versions belong to (e.g. customer_id). SCD2 integrity checks "
        "allow only one active version per key.",
    },
    "partition_column": {"label": "Partition column", "input": "column"},
    "freshness_sla_seconds": {"label": "Freshness SLA (seconds)", "input": "number"},
    # The SCD2 "active definition" — how we tell an open row from a closed one.
    "active_mode": {
        "label": "Active rows defined by",
        "input": "enum",
        "options": ["end_date", "flag"],
        "help": "end_date: effective-to is a max-future date (or NULL). flag: a current-flag equals a value.",
    },
    "high_date_value": {
        "label": "High/max-future date",
        "input": "text",
        "help": "The sentinel that marks an open version, e.g. 9999-12-31.",
    },
    "open_end_is_null": {
        "label": "Treat NULL end-date as open",
        "input": "bool",
        "help": "dbt-style snapshots leave dbt_valid_to NULL for the current row.",
    },
    "current_flag_value": {
        "label": "Active flag value",
        "input": "text",
        "help": "What the current-flag holds for an active row, e.g. Y or true or 1.",
    },
}

# The user-selectable load patterns, each mapped to a friendly label and the extra fields it
# needs. Order is the display order. "later also": add an entry (+ a classifier hint if you
# want auto-detect) and both creation surfaces pick it up.
PATTERN_SPECS: list[dict] = [
    {
        "pattern": "full_snapshot",
        "label": "Truncate & Load (full snapshot)",
        "description": "Table is fully replaced every load; its row count legitimately swings.",
        "fields": [],
    },
    {
        "pattern": "append",
        "label": "Incremental — append-only",
        "description": "New rows are appended; a watermark lets runs read only new rows.",
        "fields": ["watermark_column", "watermark_type"],
    },
    {
        "pattern": "cdc",
        "label": "Incremental — CDC (inserts + updates)",
        "description": "Rows are inserted and updated, tracked by a change timestamp.",
        "fields": ["watermark_column", "watermark_type", "changed_at_column"],
    },
    {
        "pattern": "scd2",
        "label": "SCD Type 2 (history)",
        "description": "Versioned history. 'Active' is defined either by an end-date sentinel "
        "(effective-to is a max-future date, e.g. src_eff_end_date = 9999-12-31) or a current-flag. "
        "One active version is allowed per business key.",
        "fields": [
            "effective_from_column",
            "effective_to_column",
            "current_flag_column",
            "business_key",
            "batch_id_column",
            "active_mode",
            "high_date_value",
            "open_end_is_null",
            "current_flag_value",
        ],
    },
    {
        "pattern": "partitioned",
        "label": "Partitioned",
        "description": "Loaded by partition (e.g. a date/ds column).",
        "fields": ["partition_column"],
    },
    {
        "pattern": "reference",
        "label": "Reference / lookup",
        "description": "Small, slowly-changing dimension or lookup table.",
        "fields": ["freshness_sla_seconds"],
    },
    {
        "pattern": "unknown",
        "label": "Unknown / not classified",
        "description": "Pattern not set — pillars fall back to safe defaults.",
        "fields": [],
    },
]


# Suggested historical-baseline rules per load pattern. These are *recommendations* the user
# acknowledges (and can tweak the numbers on) — never auto-applied. Each rule is a baseline rule
# on the table's row count. `params` are the tweakable knobs surfaced in the UI with their
# defaults; `config` is merged with the user's params when the rule is materialized.
# Extensible: add a pattern's suggestions here and both the API and UI pick them up.
RULE_SUGGESTIONS: dict[str, list[dict]] = {
    "full_snapshot": [
        {
            "key": "deviation_pct",
            "type": "deviation",
            "label": "Row count within ±X% of recent average",
            "rationale": "A truncate-and-load table is fully replaced each run; its count should "
            "stay close to its recent norm. This warns on an unusually large swing.",
            "severity": "warn",
            "config": {"aggregate": "avg", "window": {"observations": 30}, "direction": "both"},
            "params": [{"key": "tolerance_pct", "label": "Tolerance %", "default": 5}],
        },
    ],
    "append": [
        {
            "key": "monotonic_nondec",
            "type": "monotonic",
            "label": "Row count never decreases",
            "rationale": "An append-only table should only grow — a drop means rows were lost.",
            "severity": "fail",
            "config": {"direction": "non_decreasing"},
            "params": [],
        },
        {
            "key": "growth_band",
            "type": "growth",
            "label": "Daily growth within an expected band",
            "rationale": "Catch a stalled load (too few new rows) or a runaway load (too many).",
            "severity": "warn",
            "config": {"unit": "absolute"},
            "params": [
                {"key": "min_change", "label": "Min rows/run", "default": 1},
                {"key": "max_change", "label": "Max rows/run", "default": 1000000},
            ],
        },
    ],
    "cdc": [
        {
            "key": "monotonic_nondec",
            "type": "monotonic",
            "label": "Row count never decreases",
            "rationale": "Inserts+updates shouldn't reduce the total row count.",
            "severity": "fail",
            "config": {"direction": "non_decreasing"},
            "params": [],
        },
        {
            "key": "deviation_pct",
            "type": "deviation",
            "label": "Row count within ±X% of recent average",
            "rationale": "Warns on an abnormal change in table size between loads.",
            "severity": "warn",
            "config": {"aggregate": "avg", "window": {"observations": 30}, "direction": "both"},
            "params": [{"key": "tolerance_pct", "label": "Tolerance %", "default": 10}],
        },
    ],
    "scd2": [
        {
            "key": "monotonic_nondec",
            "type": "monotonic",
            "label": "Total row count never decreases",
            "rationale": "SCD-2 keeps history — total versions only grow.",
            "severity": "fail",
            "config": {"direction": "non_decreasing"},
            "params": [],
        },
    ],
    "partitioned": [
        {
            "key": "growth_band",
            "type": "growth",
            "label": "Each load adds rows within an expected band",
            "rationale": "A new partition should add roughly the expected number of rows.",
            "severity": "warn",
            "config": {"unit": "absolute"},
            "params": [
                {"key": "min_change", "label": "Min rows/run", "default": 1},
                {"key": "max_change", "label": "Max rows/run", "default": 1000000},
            ],
        },
    ],
    "reference": [
        {
            "key": "deviation_tight",
            "type": "deviation",
            "label": "Row count within ±X% of recent average (tight)",
            "rationale": "Reference/lookup tables change slowly; even a small swing is worth a look.",
            "severity": "warn",
            "config": {"aggregate": "avg", "window": {"observations": 30}, "direction": "both"},
            "params": [{"key": "tolerance_pct", "label": "Tolerance %", "default": 2}],
        },
    ],
}


def rule_suggestions(load_pattern: str) -> list[dict]:
    """Suggested baseline rules for a load pattern (empty for unknown/unmapped)."""
    return RULE_SUGGESTIONS.get(load_pattern or "", [])


def pattern_specs() -> dict:
    """The extensible load-pattern catalog for the creation UI: each pattern's label,
    description, the per-table fields it needs, and the suggested baseline rules."""
    return {"patterns": PATTERN_SPECS, "fields": _FIELD_SPECS, "rule_suggestions": RULE_SUGGESTIONS}


class TableMetadataService:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    # ----------------------------- injection (the external-system surface) -----------------------------
    def bulk_upsert(self, ctx: TenantContext, items: list[dict], source: str | None = None) -> dict:
        """Inject/override load-pattern metadata for many tables at once (idempotent, keyed by
        table_ref). `source` records provenance (e.g. the ingestion system's name)."""
        ctx.require(Permission.WRITE)
        if not items:
            raise ValidationError("no items to upsert")
        out = []
        with self.store.session(ctx) as s:
            for item in items:
                out.append(self._upsert_one(s, ctx, item, source))
            audit.record(
                s,
                ctx,
                "table_metadata.inject",
                entity_type="table_metadata",
                entity_id="bulk",
                detail={"count": len(out), "source": source or "user"},
            )
        return {"upserted": len(out), "items": out}

    def upsert(self, ctx: TenantContext, item: dict, source: str | None = None) -> dict:
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            d = self._upsert_one(s, ctx, item, source)
            audit.record(
                s,
                ctx,
                "table_metadata.upsert",
                entity_type="table_metadata",
                entity_id=d["table_ref"],
                detail={"source": source or "user"},
            )
            return d

    def _upsert_one(self, s, ctx: TenantContext, item: dict, source: str | None):  # noqa: ANN001
        table_ref = (item.get("table_ref") or "").strip()
        if not table_ref:
            raise ValidationError("each item needs a table_ref")
        pattern = item.get("load_pattern", "unknown")
        if pattern not in LOAD_PATTERNS:
            raise ValidationError(
                "unknown load_pattern", details={"load_pattern": pattern, "allowed": LOAD_PATTERNS}
            )
        row = s.scalar(
            select(TableMetadata).where(
                TableMetadata.group_id == ctx.group_id, TableMetadata.table_ref == table_ref
            )
        )
        if row is None:
            row = TableMetadata(org_id=ctx.org_id, group_id=ctx.group_id, table_ref=table_ref)
            s.add(row)
        # Nest flat SCD2 active-definition knobs under config.scd2 (they arrive flat from the
        # data-driven UI / injection / classifier).
        scd2 = {k: item[k] for k in _SCD2_CONFIG_KEYS if item.get(k) is not None}
        if scd2:
            cfg = dict(item.get("config") or row.config or {})
            cfg["scd2"] = {**cfg.get("scd2", {}), **scd2}
            item = {**item, "config": cfg}
        for f in _FIELDS:
            if f in item:
                setattr(row, f, item[f])
        row.source = source or item.get("source") or "user"
        row.confidence = item.get("confidence")
        row.updated_by = ctx.user_id
        s.flush()
        return _dict(row)

    # ----------------------------- read -----------------------------
    def list(self, ctx: TenantContext) -> list[dict]:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            rows = s.scalars(
                select(TableMetadata)
                .where(TableMetadata.group_id == ctx.group_id)
                .order_by(TableMetadata.table_ref)
            ).all()
            return [_dict(r) for r in rows]

    def get(self, ctx: TenantContext, table_ref: str) -> dict:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            row = self._load(s, ctx, table_ref)
            return _dict(row)

    def delete(self, ctx: TenantContext, table_ref: str) -> None:
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            s.delete(self._load(s, ctx, table_ref))
            audit.record(
                s, ctx, "table_metadata.delete", entity_type="table_metadata", entity_id=table_ref
            )

    def resolve(
        self, ctx: TenantContext, table_ref: str, monitor_load_type: str | None = None
    ) -> dict:
        """Effective metadata for a table: the per-table record if present, else a default
        derived from the monitor-level load_type. Never raises — returns `unknown` if nothing."""
        with self.store.session(ctx) as s:
            row = s.scalar(
                select(TableMetadata).where(
                    TableMetadata.group_id == ctx.group_id, TableMetadata.table_ref == table_ref
                )
            )
            if row is not None:
                return _dict(row)
        return {
            "table_ref": table_ref,
            "load_pattern": default_pattern_for(monitor_load_type),
            "source": "monitor_default",
            "resolved_from": "monitor",
        }

    def _load(self, s, ctx: TenantContext, table_ref: str) -> TableMetadata:  # noqa: ANN001
        row = s.scalar(
            select(TableMetadata).where(
                TableMetadata.group_id == ctx.group_id, TableMetadata.table_ref == table_ref
            )
        )
        if row is None:
            raise NotFoundError("table metadata not found", details={"table_ref": table_ref})
        return row


# ----------------------------- auto-classifier (fallback) -----------------------------
def classify_pattern(
    columns: list[dict], row_count: int | None = None, *, convention: dict | None = None
) -> dict:
    """Heuristically propose a load pattern from a column profile. Name-tolerant (substring/suffix)
    so prefixed real-world names like `src_eff_start_date` are recognised, SCD2 end-date aware, and
    **convention-first**: any role resolved by the monitor's ETL metadata-column convention is used
    directly before falling back to heuristics. A suggestion only — confirmed/overridden by the
    user or by external injection, never silently authoritative.

    `convention` is a role→column map (see METADATA_PRESETS); only roles whose column actually
    exists in `columns` are honoured (the convention is advisory, not every table conforms).
    """
    present = {c.get("name", "") for c in columns}
    present_low = {n.lower(): n for n in present}
    conv = {role: col for role, col in (convention or {}).items() if col in present}

    def find(role: str, *frags: str) -> str | None:
        # Convention wins when it resolves a real column; else exact, then suffix/substring.
        if conv.get(role):
            return conv[role]
        for frag in frags:
            if frag in present_low:
                return present_low[frag]
        for frag in frags:
            for low, name in present_low.items():
                if low.endswith(frag) or frag in low:
                    return name
        return None

    eff_from = find("effective_from", "effective_from", "valid_from", "eff_start", "start_date", "valid_start")
    eff_to = find("effective_to", "effective_to", "valid_to", "eff_end", "end_date", "valid_end")
    cur = find("current_flag", "is_current", "current_flag", "is_active", "active_flag", "iscurrent")
    batch = find("batch_id", "batch_id", "load_batch", "load_id", "batch")

    if eff_from and (eff_to or cur):
        mode = "flag" if (cur and not eff_to) else "end_date"
        fields = dict(
            effective_from_column=eff_from, effective_to_column=eff_to,
            current_flag_column=cur, batch_id_column=batch, active_mode=mode,
        )
        if mode == "end_date":
            fields.update(high_date_value=_DEFAULT_HIGH_DATE, open_end_is_null=True)
        else:
            fields.update(current_flag_value="Y")
        return _suggest("scd2", 0.9 if (eff_to and cur) else 0.85, **{k: v for k, v in fields.items() if v is not None})

    changed = find("updated_at", "updated_at", "modified_at", "last_modified", "change_ts", "_updated_at")
    if changed:
        return _suggest("cdc", 0.8, watermark_column=changed, watermark_type="timestamp",
                        changed_at_column=changed, batch_id_column=batch)

    part = find("partition", "dt", "ds", "partition_date", "partition_key", "load_date", "event_date")
    if part:
        return _suggest("partitioned", 0.75, partition_column=part)

    appended = find("loaded_at", "load_ts", "ingested_at", "created_at", "event_time", "_loaded_at", "inserted_at")
    if appended:
        return _suggest("append", 0.7, watermark_column=appended, watermark_type="timestamp",
                        batch_id_column=batch)

    # no temporal signal → reference if small, else a replaced snapshot
    if row_count is not None and row_count <= 10000:
        return _suggest("reference", 0.5, freshness_sla_seconds=7 * 24 * 3600)
    return _suggest("full_snapshot", 0.5)


def _suggest(pattern: str, confidence: float, **fields) -> dict:
    return {"load_pattern": pattern, "confidence": confidence,
            **{k: v for k, v in fields.items() if v is not None}}


def _dict(r: TableMetadata) -> dict:
    cfg = r.config or {}
    scd2 = cfg.get("scd2", {}) if isinstance(cfg, dict) else {}
    meta = {
        "table_ref": r.table_ref,
        "load_pattern": r.load_pattern,
        "watermark_column": r.watermark_column,
        "watermark_type": r.watermark_type,
        "partition_column": r.partition_column,
        "effective_from_column": r.effective_from_column,
        "effective_to_column": r.effective_to_column,
        "current_flag_column": r.current_flag_column,
        "changed_at_column": r.changed_at_column,
        "batch_id_column": r.batch_id_column,
        "business_key": r.business_key or [],
        "freshness_sla_seconds": r.freshness_sla_seconds,
        "config": cfg,
        "source": r.source,
        "confidence": r.confidence,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }
    # Flatten the SCD2 knobs to top-level so the data-driven UI round-trips them as plain fields.
    for k in _SCD2_CONFIG_KEYS:
        if k in scd2:
            meta[k] = scd2[k]
    if r.load_pattern == "scd2":
        meta["active_predicate"] = scd2_active_predicate(meta)
    return meta
