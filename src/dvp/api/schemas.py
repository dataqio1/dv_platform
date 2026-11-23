"""Pydantic request DTOs for the API. Response bodies use ApiResponse[dict]."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SampleStoreUpdate(BaseModel):
    """Where compare exception rows are written. `schema` is the warehouse schema."""

    connection_id: str
    database: str
    schema_name: str = Field(alias="schema")
    table: str = "DIFF_SAMPLES"

    model_config = {"populate_by_name": True}


class LineageEdgeIn(BaseModel):
    upstream: str
    downstream: str


class LineageEdges(BaseModel):
    edges: list[LineageEdgeIn]


class DbtRunIngest(BaseModel):
    """A dbt run_results.json body. Extra keys from the artifact are ignored."""

    results: list[dict]
    metadata: dict = Field(default_factory=dict)
    invocation_id: str | None = None


class ConnectionCreate(BaseModel):
    source_type: str = Field(examples=["snowflake", "oracle"])
    name: str
    coordinates: dict = Field(default_factory=dict)
    auth_method: str = Field(examples=["key_pair", "oauth_sso", "external_browser", "password"])
    secret: dict | None = None
    allowed_scopes: dict = Field(default_factory=dict)
    # Warehouse-wide ETL metadata-column convention monitors inherit: {"preset": "...",
    # "roles": {role: column}}. See docs/design/11.
    metadata_columns: dict | None = None
    # Named conventions attached to this connection (ids or `sys:<preset>`); monitors inherit them.
    metadata_convention_ids: list[str] | None = None


class FileDatasetInfo(BaseModel):
    """One candidate file dataset discovered in an S3/file connection (Figma screen 4)."""

    uri: str | None = None
    table_name: str
    format: str | None = Field(default=None, examples=["csv", "parquet", "ndjson"])
    columns_count: int | None = None
    schema_hash: str | None = None
    row_count: int | None = None
    verification_status: str = Field(examples=["verified", "blocked", "unverified", "outdated"])
    blocked_reason: str | None = None
    arrival_window: str | None = None
    last_modified: str | None = None


class FileDatasetListResponse(BaseModel):
    """Paginated discovery result for GET /connections/{id}/file-datasets."""

    connection_id: str
    datasets: list[FileDatasetInfo]
    total: int
    page: int
    size: int
    has_more: bool


class FileDatasetVerifyRequest(BaseModel):
    """Bulk-verify selected file datasets — fetch + hash each schema and persist its state.
    Each entry may be a bare dataset name, a FQN, or a file URI."""

    datasets: list[str] = Field(examples=[["ORDERS", "CUSTOMERS"]])


class FreshnessPolicy(BaseModel):
    """When new data is expected to arrive (this is the freshness pillar).

    `source` picks how freshness is measured: `watermark` = MAX(watermark_column);
    `file_arrival` = newest file's last-modified time (S3/local files); `auto` =
    watermark when a column is set, else file arrival when the connector supports it."""

    source: str = Field(default="auto", examples=["auto", "watermark", "file_arrival"])
    mode: str = Field(default="staleness", examples=["staleness", "deadline"])
    cadence_hours: float = Field(default=24, gt=0, description="How often new data is expected")
    warn_after_hours: float = Field(default=24, gt=0, description="Warn when data older than this")
    severe_after_hours: float = Field(
        default=72, gt=0, description="Escalate to severe past this age"
    )
    deadline_time: str | None = Field(
        default=None, examples=["06:00"], description="HH:MM UTC daily deadline (mode=deadline)"
    )
    grace_minutes: float = Field(default=30, ge=0, description="Grace after the deadline")


class QualityPolicy(BaseModel):
    warn_below: float = Field(default=80, ge=0, le=100)
    severe_below: float = Field(default=60, ge=0, le=100)


class VolumePolicy(BaseModel):
    """Optional % deviation thresholds. When unset, volume severity uses the anomaly z-score."""

    warn_pct: float | None = Field(default=None, ge=0)
    severe_pct: float | None = Field(default=None, ge=0)


class SchemaPolicy(BaseModel):
    """Severity to apply to each kind of schema change (the schema pillar)."""

    added: str = Field(default="error", examples=["error", "warn", "ignore"])
    removed: str = Field(default="error", examples=["error", "warn", "ignore"])
    type_changed: str = Field(default="error", examples=["error", "warn", "ignore"])


class DistributionPolicy(BaseModel):
    """Drift thresholds for the distribution pillar (PSI/KL/JS against the approved reference)."""

    method: str = Field(default="PSI", examples=["PSI", "KL", "JS"])
    warn: float = Field(default=0.1, ge=0)
    error: float = Field(default=0.25, ge=0)


class AlertPolicy(BaseModel):
    """Per-monitor alert policy — applies to every table and file in the monitor."""

    freshness: FreshnessPolicy = Field(default_factory=FreshnessPolicy)
    quality: QualityPolicy = Field(default_factory=QualityPolicy)
    volume: VolumePolicy = Field(default_factory=VolumePolicy)
    schema_: SchemaPolicy = Field(default_factory=SchemaPolicy, alias="schema")
    distribution: DistributionPolicy = Field(default_factory=DistributionPolicy)

    model_config = {"populate_by_name": True}


class MonitorCreate(BaseModel):
    connection_id: str
    name: str
    scope: dict = Field(
        default_factory=dict,
        examples=[{"database": "DB", "schema": "PUBLIC", "tables": ["ORDERS"]}],
    )
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    schedule_cron: str | None = None
    # hourly | daily (06:00 UTC) | manual. Wins over schedule_cron when set.
    schedule: str | None = None
    watermark_column: str | None = Field(default=None, examples=["INGESTED_AT"])
    watermark_type: str | None = Field(default=None, examples=["timestamp", "date", "numeric"])
    load_type: str = Field(default="incremental", examples=["incremental", "full_reload", "append"])
    # How far back (days) to analyze data for baselines/charts. Defaults to 30 days.
    lookback_days: int = Field(default=30, ge=1, le=400)
    # Freshness arrival window + per-pillar severity thresholds. Omit for engine defaults.
    alert_policy: AlertPolicy | None = None
    project_id: str | None = None
    source_system: str | None = None
    layer: str | None = Field(default=None, examples=["bronze", "silver", "gold"])
    # ETL metadata-column convention for this monitor; inherits the connection's default when
    # omitted. {"preset": "...", "roles": {role: column}}. See docs/design/11.
    metadata_columns: dict | None = None
    # Named conventions for this monitor; inherits the connection's when omitted.
    metadata_convention_ids: list[str] | None = None


class MonitorUpdate(BaseModel):
    """Rename a monitor, set its description, or change its schedule. Omitted fields stay.
    `schedule_cron` accepts a raw 5-field cron (from the UI's schedule picker); `schedule`
    is the legacy preset name. Pass "" or null cron for Manual."""

    name: str | None = None
    description: str | None = None
    schedule: str | None = Field(default=None, examples=["hourly", "daily", "manual"])
    schedule_cron: str | None = Field(default=None, examples=["0 6 * * *", "30 2 * * 1"])
    lookback_days: int | None = Field(default=None, ge=1, le=400)
    alert_policy: AlertPolicy | None = None
    metadata_columns: dict | None = None


class MonitorStatus(BaseModel):
    status: str = Field(examples=["active", "paused", "archived"])


class MonitorWatermark(BaseModel):
    """Set/clear the monitor-level watermark column (Edit Monitor → Watermark)."""

    watermark_column: str | None = Field(default=None, examples=["INGESTED_AT"])
    watermark_type: str | None = Field(
        default="timestamp", examples=["timestamp", "date", "numeric"]
    )


class MonitorLoadType(BaseModel):
    """How the monitored table is loaded (drives expected volume behavior)."""

    load_type: str = Field(examples=["incremental", "full_reload", "append"])


class WatermarkDetectRequest(BaseModel):
    scope: dict = Field(
        default_factory=dict,
        examples=[
            {"database": "RAW_SALES", "schema": "PUBLIC", "tables": ["RAW_CUSTOMERS", "RAW_ORDERS"]}
        ],
    )


class WatermarkValidateRequest(BaseModel):
    scope: dict = Field(default_factory=dict)
    column: str = Field(examples=["INGESTED_AT"])
    expected_type: str | None = Field(default=None, examples=["timestamp"])


class LoadTypeAlternative(BaseModel):
    """A plausible-but-not-chosen load type the user can switch to."""

    load_type: str = Field(examples=["scd2", "append", "full_reload"])
    reason: str


class LoadTypeCandidate(BaseModel):
    """How one table in the monitor was classified + the columns suggested per role."""

    table_ref: str
    load_type: str = Field(examples=["scd1", "scd2", "append", "full_reload", "other"])
    load_pattern: str = Field(examples=["cdc", "scd2", "append", "full_snapshot"])
    confidence: float = Field(ge=0, le=1)
    reason: str
    # Best single column suggested for each role (null when none matched).
    column_mappings: dict[str, str | None]
    # Every plausible column per role (the picker's dropdown options).
    column_candidates: dict[str, list[str]]
    alternatives: list[LoadTypeAlternative]


class LoadTypeDetectionResult(LoadTypeCandidate):
    """Alias kept for symmetry with the per-table result shape."""


class LoadTypeDetectionResponse(BaseModel):
    """Result of POST /monitors/{id}:detect-load-type — one entry per scoped table."""

    monitor_id: str
    connection_id: str
    tables_inspected: list[str]
    watermark: dict | None = None
    results: list[LoadTypeCandidate]


class SnapshotObservation(BaseModel):
    """A point-in-time profiling observation for a table (the profiler's write path)."""

    row_count: int | None = None
    last_load_ts: str | None = Field(default=None, examples=["2026-09-18T10:45:00Z"])
    columns: list[dict] | None = Field(
        default=None,
        examples=[[{"name": "EMAIL", "data_type": "STRING", "null_rate": 0.02}]],
    )
    quality_score: float | None = Field(default=None, ge=0, le=100)
    rows_analyzed: int | None = None
    query_shape: str | None = None
    ts: str | None = None


class RuleCreate(BaseModel):
    name: str
    type: str = Field(
        examples=[
            "not_null",
            "null_rate",
            "unique",
            "range",
            "regex",
            "freshness",
            "custom_sql",
            "metric",
        ]
    )
    description: str | None = None
    columns: list[str] = Field(default_factory=list)
    config: dict = Field(
        default_factory=dict,
        examples=[
            {
                "expression": "SELECT COUNT(*) AS v FROM {{table}} WHERE amount < 0",
                "pass_when": "value == 0",
                "threshold": 0,
                "dimension": "quality",
            }
        ],
    )
    severity: str = "warning"
    scope: str = "incremental"
    state: str = "enabled"
    table_ref: str | None = None
    confidence: float | None = None


class RuleResultRecord(BaseModel):
    """One evaluation of a rule (computed by the executor / pushdown, or pushed for testing)."""

    table_ref: str | None = None
    metric_value: float | None = None
    threshold: float | None = None
    passed: bool | None = None  # derived from value vs threshold when omitted
    observed: dict = Field(default_factory=dict)
    run_id: str | None = None
    bad_sample_ref: str | None = None
    ts: str | None = None


class RuleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    config: dict | None = None
    severity: str | None = None
    scope: str | None = None
    state: str | None = None
    columns: list[str] | None = None


class AutogenRequest(BaseModel):
    level: int = Field(ge=1, le=3)
    # For the scaffold, the caller may pass a profile directly; in production this
    # comes from profiling the table.
    profile: dict | None = None


class SuggestPack(BaseModel):
    """Auto-generate a suggested DQ rule pack for a monitor's tables from a SAMPLE of the last
    batch (never a full scan). Rules land as state='suggested', origin='system' — the user accepts
    or removes them. `tables` (names, not FQNs) picks a subset; omit for every table in scope."""

    tables: list[str] | None = None
    sample_size: int = Field(default=1000, ge=1, le=100000)
    level: int = Field(default=2, ge=1, le=3)
    persist: bool = True  # false → preview only (specs + dry-run, nothing created)


class CompareCreate(BaseModel):
    kind: str = Field(examples=["count", "schema", "data"])
    name: str | None = None
    # Attach this test case to an existing batch (used when editing a batch adds a new pair).
    batch_id: str | None = None
    batch_name: str | None = None
    # Every test case lives in a project; omit to use the group's default ("General").
    project_id: str | None = None
    source_system: str | None = None
    layer: str | None = Field(default=None, examples=["bronze", "silver", "gold"])
    tags: list[str] = Field(default_factory=list)
    source: dict = Field(
        examples=[{"connection_id": "…", "database": "RAW", "schema": "PUBLIC", "table": "ORDERS"}]
    )
    target: dict
    options: dict = Field(
        default_factory=dict,
        examples=[{"key": ["ORDER_ID"], "tolerance_pct": 0, "null_equals_empty": True}],
    )


class CompareUpdate(BaseModel):
    """Partial edit of a test case's definition. Only the fields present in the request are
    changed — omit a field to leave it as-is. Taxonomy (project/system/layer/tags) is edited
    separately via the assignment endpoint; schedule via the schedule endpoint."""

    name: str | None = None
    batch_name: str | None = None
    kind: str | None = Field(default=None, examples=["count", "schema", "data"])
    source: dict | None = None
    target: dict | None = None
    options: dict | None = None


class SampleByKey(BaseModel):
    """Preview sample data for a pair: pick N keys from source, fetch those rows from both sides."""

    source: dict
    target: dict
    key: list[str] = []  # empty → return an independent sample from each side (unaligned)
    # Explicit key values to look up instead of sampling from the source. Each inner list is one
    # key tuple whose values line up with `key` (so [["1001","US"], ...] for a 2-column key).
    # None/empty → sample ~limit keys from the source (the original behaviour).
    key_values: list[list] | None = None
    columns: list[str] | None = None
    column_mappings: dict[str, str] | None = None
    limit: int = 20


class SuggestFromSample(BaseModel):
    """Generate suggested DQ rules from a sample of the monitor's dataset (not saved)."""

    level: int = Field(default=2, ge=1, le=3)  # 1=Essential, 2=Standard, 3=Comprehensive
    sample_size: int = 10000


class DryRunRules(BaseModel):
    """Evaluate rule specs against a sample without persisting — pass/fail + failing examples."""

    rules: list[dict]
    sample_size: int = 10000


class DryRunCompare(BaseModel):
    """Run the real compare on a small sample (default 1,000 rows) without persisting."""

    source: dict
    target: dict
    key: list[str]
    columns: list[str] | None = None
    column_mappings: dict[str, str] | None = None
    limit: int = 1000


class RunCompareRequest(BaseModel):
    """Optional runtime variables for a test-case execution (fills {{var}} placeholders)."""

    variables: dict = Field(
        default_factory=dict, examples=[{"env": "PROD", "run_date": "2026-10-01"}]
    )


class SuggestMappingRequest(BaseModel):
    """Preview detected keys + column mapping for a source/target pair before running."""

    source: dict = Field(examples=[{"connection_id": "…", "schema": "RAW", "table": "ORDERS"}])
    target: dict = Field(examples=[{"connection_id": "…", "schema": "STG", "table": "ORDERS_DIM"}])
    sample_size: int = Field(default=200, ge=1, le=5000)
    # Column mapping is cheap (metadata only); key detection samples data and can be slow — so
    # the UI maps columns first and only detects keys on explicit request.
    detect_key: bool = True
    # "fuzzy" = match on column names only (fast); "deep" = also sample data and match leftovers
    # by value overlap (slower, probabilistic — the user confirms).
    mode: str = "fuzzy"


class DetectKeysRequest(BaseModel):
    """Detect candidate key columns for one dataset (samples data — may be slow)."""

    source: dict = Field(examples=[{"connection_id": "…", "schema": "RAW", "table": "ORDERS"}])
    target: dict | None = Field(default=None, examples=[{"connection_id": "…", "table": "ORDERS_DIM"}])


class CompareBatchCreate(BaseModel):
    """Run one compare per table pair (up to 100), continue-on-error → partial."""

    kind: str = Field(examples=["count", "schema", "data"])
    name: str = Field(examples=["nightly reconciliation"])
    project_id: str | None = None  # omit to use the group's default ("General") project
    source: dict = Field(examples=[{"connection_id": "…", "database": "PROD", "schema": "PUBLIC"}])
    target: dict = Field(examples=[{"connection_id": "…", "database": "STG", "schema": "PUBLIC"}])
    tables: list[str] | None = Field(default=None, examples=[["ORDERS", "CUSTOMERS", "ITEMS"]])
    table_pairs: list[dict] | None = Field(
        default=None, examples=[[{"source_table": "ORD", "target_table": "ORDERS"}]]
    )
    options: dict = Field(default_factory=dict)
    variables: dict = Field(default_factory=dict)


class ReportCreate(BaseModel):
    """Create a report. `spec` filters what it aggregates (batch_id, job_ids, kinds,
    monitor_ids, since_days) — empty means everything in the group."""

    name: str = Field(examples=["Nightly reconciliation — 2026-10-08"])
    report_type: str = Field(examples=["data_validation", "data_quality", "executive"])
    project_id: str | None = None
    spec: dict = Field(default_factory=dict, examples=[{"batch_id": "…", "since_days": 7}])
    schedule_cron: str | None = Field(default=None, examples=["0 7 * * *"])
    delivery: dict = Field(
        default_factory=dict, examples=[{"email": ["lead@acme.io"], "slack": ["#data-health"]}]
    )


class ProjectCreate(BaseModel):
    """A project groups test cases (compare jobs + monitors) for management + reporting."""

    name: str = Field(examples=["Finance warehouse migration"])
    description: str | None = None
    tags: list[str] = Field(default_factory=list, examples=[["migration", "finance"]])
    # Project-level {{variables}} inherited by every test case (overridden by job/run-time values).
    variables: dict[str, str] | None = Field(default=None, examples=[{"env": "PROD"}])


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    variables: dict[str, str] | None = None


class TagCreate(BaseModel):
    """A first-class, managed label. Entities reference tags by name."""

    name: str = Field(examples=["critical", "pii", "finance"])
    color: str | None = Field(default=None, examples=["#ef4444"])
    description: str | None = None


class TagUpdate(BaseModel):
    name: str | None = None
    color: str | None = None
    description: str | None = None


class TagAssignment(BaseModel):
    """Set the project / grouping / tags on a test case (compare job or monitor)."""

    project_id: str | None = None
    source_system: str | None = None
    layer: str | None = None
    tags: list[str] | None = None


class ScheduleRequest(BaseModel):
    """Set or clear a test case's recurring cron schedule. None/"" clears it (manual-only)."""

    schedule_cron: str | None = None


class TableMetadataItem(BaseModel):
    """One table's load-pattern metadata (injectable from an external ingestion system)."""

    # Optional: the single-table PUT /table-metadata/{table_ref} supplies it from the path;
    # bulk injection provides it per item (the service validates presence either way).
    table_ref: str | None = Field(default=None, examples=["ANALYTICS.PUBLIC.RAW_ORDERS"])
    load_pattern: str = Field(
        default="unknown",
        examples=["append", "cdc", "full_snapshot", "scd2", "partitioned", "reference"],
    )
    watermark_column: str | None = None
    watermark_type: str | None = None
    partition_column: str | None = None
    effective_from_column: str | None = None
    effective_to_column: str | None = None
    current_flag_column: str | None = None
    changed_at_column: str | None = None
    batch_id_column: str | None = None
    business_key: list[str] | None = None
    # SCD2 "active definition" knobs — flat here, nested under config.scd2 server-side.
    active_mode: str | None = Field(default=None, examples=["end_date", "flag"])
    high_date_value: str | None = Field(default=None, examples=["9999-12-31"])
    open_end_is_null: bool | None = None
    current_flag_value: str | None = Field(default=None, examples=["Y", "true", "1"])
    freshness_sla_seconds: int | None = None
    config: dict = Field(default_factory=dict)
    confidence: float | None = None


class MetadataConventionCreate(BaseModel):
    """A named, reusable ETL metadata-column convention (role→column), e.g. 'IDMC-CDC'."""

    name: str = Field(examples=["IDMC-CDC", "S3-Loader"])
    roles: dict = Field(examples=[{"updated_at": "AUDIT_UPDATE_TS", "batch_id": "TASK_RUN_ID"}])
    scd2: dict | None = None


class MetadataConventionUpdate(BaseModel):
    name: str | None = None
    roles: dict | None = None
    scd2: dict | None = None


class FeedbackCreate(BaseModel):
    """In-app feedback from any page. `page`/`url`/`user_agent`/`context` are captured by the
    widget so an admin can act on it without a repro."""

    message: str = Field(examples=["The compare results table is hard to read on mobile"])
    category: str = Field(default="idea", examples=["idea", "issue", "praise", "other"])
    rating: int | None = Field(default=None, ge=1, le=5)
    page: str | None = Field(default=None, examples=["/observability"])
    url: str | None = None
    user_agent: str | None = None
    context: dict | None = None


class FeedbackUpdate(BaseModel):
    status: str = Field(examples=["new", "reviewed", "closed"])


class MetadataColumnsDetect(BaseModel):
    """Detect ETL metadata columns across a monitor's scope (first-step picker). Returns a
    per-table coverage + suggested load pattern. `preset`/`convention` are optional hints."""

    scope: dict = Field(
        default_factory=dict,
        examples=[{"database": "DW", "schema": "GOLD", "tables": ["CUSTOMER_DIM"]}],
    )
    preset: str | None = Field(default=None, examples=["dbt_snapshot", "informatica_dw"])
    convention: dict | None = None  # explicit role→column map; overrides the preset's roles


class TableMetadataInject(BaseModel):
    """Bulk upsert of table load-pattern metadata from an external system."""

    source: str | None = Field(default=None, examples=["acme_ingestion", "dbt", "user"])
    items: list[TableMetadataItem]


class MemberAdd(BaseModel):
    """Add a user to the active group with a role (admin action)."""

    email: str = Field(examples=["analyst@acme.io"])
    role: str = Field(default="viewer", examples=["report_viewer", "viewer", "editor", "group_admin"])


class MemberRoleUpdate(BaseModel):
    role: str = Field(examples=["report_viewer", "viewer", "editor", "group_admin"])


class IntegrationCreate(BaseModel):
    """Create a tenant notification channel. `values` holds all provider fields (secret
    ones are split out to the SecretStore server-side)."""

    provider: str = Field(examples=["email", "slack", "teams", "webhook"])
    name: str = Field(examples=["#data-alerts"])
    values: dict = Field(default_factory=dict)
    enabled: bool = True


class IntegrationUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    values: dict | None = None


class RouteCreate(BaseModel):
    event: str = Field(default="incident_opened", examples=["incident_opened"])
    min_severity: str = Field(default="warning", examples=["info", "warning", "critical"])
    integration_id: str
    enabled: bool = True


class RouteUpdate(BaseModel):
    min_severity: str | None = Field(default=None, examples=["warning", "critical"])
    enabled: bool | None = None


class MetricCreate(BaseModel):
    """A historical-baseline metric. Phase 1: SQL source on an existing connection."""

    name: str = Field(examples=["claims_fact_row_count"])
    source_type: str = Field(default="sql", examples=["sql", "file", "dashboard"])
    source_config: dict = Field(
        default_factory=dict,
        examples=[{"query": "SELECT COUNT(*) AS v FROM ANALYTICS.CLAIMS_FACT"}],
    )
    connection_id: str | None = None
    description: str | None = None
    monitor_id: str | None = None  # attach to a monitor (observability)
    project_id: str | None = None  # attach to a project (validation)
    fallback_threshold: float | None = None
    retention_days: int = 400
    timezone: str = "UTC"
    schedule_cron: str | None = Field(default=None, examples=["0 6 * * *"])  # null = manual


class MetricRuleCreate(BaseModel):
    name: str = Field(examples=["within ±5% of 30-day avg"])
    type: str = Field(examples=["deviation", "monotonic", "growth"])
    config: dict = Field(default_factory=dict)
    severity: str = Field(default="warn", examples=["warn", "fail"])


class MetricRunRequest(BaseModel):
    run_id: str | None = None
    run_date: str | None = Field(default=None, examples=["2026-10-17"])
    business_date: str | None = Field(default=None, examples=["2026-10-17"])


class ObservationExclude(BaseModel):
    reason: str = Field(default="", examples=["known outage"])


class MetricReset(BaseModel):
    reset_at: str = Field(examples=["2026-10-15"])
    reason: str | None = None


class IncidentComment(BaseModel):
    body: str = Field(examples=["Investigated — this was an expected backfill."])


class IncidentResolve(BaseModel):
    """Resolve an alert with a disposition. 'ok' = valid but accepted/expected, 'false_positive'
    = not a real problem, 'confirmed' = a real issue. Optional note is kept as a comment."""

    resolution: str = Field(default="ok", examples=["ok", "false_positive", "confirmed"])
    note: str | None = None


class IncidentSilence(BaseModel):
    """Suppress alerting for an incident (scope=incident) or every new incident on its monitor
    (scope=monitor). Provide either an absolute `until` (ISO-8601) or a `duration_hours`."""

    scope: str = Field(default="incident", examples=["incident", "monitor"])
    until: str | None = Field(default=None, examples=["2026-10-25T00:00:00Z"])
    duration_hours: float | None = Field(default=None, examples=[24])
    note: str | None = None
    ticket_url: str | None = Field(default=None, examples=["https://jira/DQ-123"])


class OrgSettingsUpdate(BaseModel):
    """Free-form JSON settings blob for a namespaced key (organization/auth/governance). The UI
    owns the shape; the backend stores and returns it verbatim (no secrets)."""

    value: dict = Field(default_factory=dict)


class ServiceAccountCreate(BaseModel):
    name: str = Field(examples=["ci-pipeline"])
    role: str = Field(default="viewer", examples=["viewer", "editor", "group_admin"])


class PointExclude(BaseModel):
    excluded: bool = True


class TableSuggestionSelection(BaseModel):
    key: str = Field(examples=["deviation_pct"])
    params: dict = Field(default_factory=dict, examples=[{"tolerance_pct": 5}])
    severity: str | None = None


class AcknowledgeTableSuggestions(BaseModel):
    """Acknowledge load-pattern rule suggestions for a table → create a row-count metric + rules."""

    monitor_id: str
    table_ref: str = Field(examples=["DWH.PUBLIC.ORDERS"])
    connection_id: str
    load_pattern: str = Field(examples=["full_snapshot", "append", "cdc"])
    selections: list[TableSuggestionSelection]


class FindColumnRequest(BaseModel):
    """Find Column: locate where one or more values live, then preview rows.

    Scope is optional and widening: omit `connection_id`/`connection_ids` to search EVERY
    readable connection, and omit `schema`/`schemas` to search every schema in each."""

    connection_id: str | None = None
    connection_ids: list[str] | None = None
    values: list[str] = Field(min_length=1, examples=[["27519", "NC"]])
    schema_name: str | None = Field(default=None, alias="schema", examples=["PUBLIC"])
    schemas: list[str] | None = None
    database: str | None = None
    tables: list[str] | None = None
    match_mode: str = Field(default="any", examples=["any", "co_occurrence"])
    sample_limit: int = Field(default=20, ge=1, le=100)
    max_tables: int = Field(default=200, ge=1, le=1000)
    use_index: str = Field(default="auto", examples=["auto", "never"])
    # Max candidate tables confirmed in parallel (source queries run concurrently). 1 = serial.
    confirm_concurrency: int = Field(default=4, ge=1, le=32)

    model_config = {"populate_by_name": True}


class FindColumnIndexBuild(BaseModel):
    """Build/refresh the Find Column Bloom index for a connection."""

    connection_id: str
    schema_name: str | None = Field(default=None, alias="schema")
    tables: list[str] | None = None
    watermark_column: str | None = None
    incremental: bool = False
    background: bool = False
    # Skip columns whose values average longer than this (large free-text). Binary/LOB types are
    # always skipped regardless.
    max_text_len: int = Field(default=100, ge=1, le=100_000)

    model_config = {"populate_by_name": True}


class FindColumnIndexConfig(BaseModel):
    """Schedule for a connection's index refresh (daily, picked up by the scheduler)."""

    connection_id: str
    schema_name: str | None = Field(default=None, alias="schema")
    tables: list[str] | None = None
    watermark_column: str | None = None
    frequency: str = Field(default="daily", examples=["daily", "manual"])
    enabled: bool = True

    model_config = {"populate_by_name": True}
