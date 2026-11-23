"""ORM models — a working subset of the data model in docs/design/07-data-model.md.

JSONB-style flexible config is stored via SQLAlchemy JSON (portable across SQLite
and Postgres). Secrets are NEVER stored here — only `secret_ref` pointers.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dvp.db.base import Base, TenantMixin, TimestampMixin, new_id, utcnow


# ----------------------------- tenancy & identity -----------------------------
class Organization(Base, TimestampMixin):
    __tablename__ = "organization"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255))
    plan: Mapped[str] = mapped_column(String(50), default="standard")


class Group(Base, TimestampMixin):
    __tablename__ = "group"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey("organization.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    isolation_tier: Mapped[str] = mapped_column(String(50), default="shared")
    quotas: Mapped[dict] = mapped_column(JSON, default=dict)
    # The LDAP group this tenant is bound to (JIT-provisioned). NULL = manually managed.
    ldap_group: Mapped[str | None] = mapped_column(String(512), index=True, nullable=True)
    # Where compare exception rows are written. Null keeps them on the platform result
    # store. A Snowflake location means the row values stay in the customer's account.
    sample_store: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class User(Base, TimestampMixin):
    __tablename__ = "user"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    external_idp_sub: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Per-user UI preferences (last project, last section, page sizes, …). A single JSON
    # blob keyed by the frontend — follows the user across devices, scoped to the user.
    preferences: Mapped[dict] = mapped_column(JSON, default=dict)


class Membership(Base, TimestampMixin):
    __tablename__ = "membership"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    group_id: Mapped[str] = mapped_column(ForeignKey("group.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), index=True)
    role: Mapped[str] = mapped_column(String(50))


# ----------------------------- tags & projects -----------------------------
class Tag(Base, TenantMixin, TimestampMixin):
    """First-class, managed label. Entities (projects, test cases) reference tags by name in
    their `tags` list; this registry owns the canonical set (name + color + description) so
    tags can be created, renamed, recolored, and deleted centrally."""

    __tablename__ = "tag"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), index=True)
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class Project(Base, TenantMixin, TimestampMixin):
    """Groups test cases (compare jobs + monitors) so they can be managed and reported on
    together. Reports are typically scoped to a project. `tags` are free-form labels."""

    __tablename__ = "project"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    # Project-level {{variables}}: a base layer merged into every test-case run in the project,
    # overridden by job-level and run-time variables. So teams set env/run_date once per project.
    variables: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Soft delete: archived projects (and their test cases) are hidden but retained and
    # restorable — a test case is never orphaned when its project is removed.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ----------------------------- connections -----------------------------
class Connection(Base, TenantMixin, TimestampMixin):
    __tablename__ = "connection"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_type: Mapped[str] = mapped_column(String(50))  # snowflake | oracle | file
    name: Mapped[str] = mapped_column(String(255))
    coordinates: Mapped[dict] = mapped_column(JSON, default=dict)
    auth_method: Mapped[str] = mapped_column(String(50))  # key_pair | oauth_sso | ...
    secret_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    allowed_scopes: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(50), default="active")
    # Warehouse-wide ETL metadata-column convention (preset name + role→column map) that every
    # monitor on this connection inherits. See docs/design/11. {} / null = none set.
    metadata_columns: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Named conventions attached to this connection (ids into MetadataConvention or `sys:<preset>`).
    # Monitors inherit these; a connection loaded by 2 ETL tools attaches both. docs/design/11 §3.4.
    metadata_convention_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


# ----------------------------- monitors & rules -----------------------------
class Monitor(Base, TenantMixin, TimestampMixin):
    __tablename__ = "monitor"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connection.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    # Organization: which project this monitor (DQ test case) belongs to + grouping taxonomy.
    project_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    source_system: Mapped[str | None] = mapped_column(String(128), nullable=True)
    layer: Mapped[str | None] = mapped_column(String(32), nullable=True)  # bronze|silver|gold|…
    scope: Mapped[dict] = mapped_column(JSON, default=dict)  # {database, schema, tables|pattern}
    schedule_cron: Mapped[str | None] = mapped_column(String(100), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    status: Mapped[str] = mapped_column(String(50), default="active")
    # Monitor-level watermark (high-water-mark incremental processing). Set once for
    # every table in the monitor so runs only scan rows past the last seen value.
    watermark_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    watermark_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # timestamp|date|numeric
    # How the table is loaded — drives expected volume behavior (a truncate-and-load table
    # legitimately swings each run; an incremental one should mostly grow).
    load_type: Mapped[str] = mapped_column(
        String(20), default="incremental"
    )  # incremental | full_reload | append
    # How far back (days) this monitor analyzes data — the baseline/history window the charts
    # and anomaly detection use. Default 30 days.
    lookback_days: Mapped[int] = mapped_column(Integer, default=30)
    # Per-monitor alert policy — freshness arrival cadence/grace and per-pillar severity
    # thresholds. Applies to every table AND file in the monitor. See observability.alertpolicy
    # for the shape; {} means "use engine defaults" (warn 24h/sev 72h freshness, <80/<60 quality).
    alert_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    # ETL metadata-column convention for this monitor (inherited from the connection unless
    # overridden): {"preset": "...", "roles": {role: column}}. See docs/design/11.
    metadata_columns: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Named conventions for this monitor (inherited from the connection unless overridden).
    metadata_convention_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    rules: Mapped[list[DQRule]] = relationship(
        back_populates="monitor", cascade="all, delete-orphan"
    )


class DQRule(Base, TenantMixin, TimestampMixin):
    __tablename__ = "dq_rule"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    monitor_id: Mapped[str] = mapped_column(ForeignKey("monitor.id"), index=True)
    table_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(String(64))
    columns: Mapped[list] = mapped_column(JSON, default=list)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    scope: Mapped[str] = mapped_column(String(20), default="incremental")  # incremental | full
    state: Mapped[str] = mapped_column(String(20), default="enabled")  # suggested|enabled|disabled
    # Provenance label (who/what authored the rule), shown in the UI. Distinct from `state`:
    #   system    — generated automatically from the detected load type
    #   suggested — profiler/level-based suggestion awaiting acceptance
    #   user      — created or edited by a person
    #   injected  — pushed in by an external ingestion/governance system
    origin: Mapped[str] = mapped_column(String(16), default="user")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    monitor: Mapped[Monitor] = relationship(back_populates="rules")


# ----------------------------- runs, results, incidents -----------------------------
class MonitorRun(Base, TenantMixin, TimestampMixin):
    __tablename__ = "monitor_run"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    monitor_id: Mapped[str] = mapped_column(ForeignKey("monitor.id"), index=True)
    table_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    ruleset_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    partition_predicate: Mapped[str | None] = mapped_column(Text, nullable=True)
    rows_analyzed: Mapped[int] = mapped_column(Integer, default=0)
    # check = MAX(watermark) + a cheap count. full_profile = a column scan.
    query_shape: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class RuleResult(Base, TimestampMixin):
    __tablename__ = "rule_result"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # Nullable: a rule can be evaluated ad-hoc (not inside a full monitor run) — e.g. a
    # custom-rule result recorded via the :record capture path (docs/design/10 §5.3).
    monitor_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("monitor_run.id"), index=True, nullable=True
    )
    dq_rule_id: Mapped[str] = mapped_column(ForeignKey("dq_rule.id"), index=True)
    table_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    rule_version: Mapped[int] = mapped_column(Integer, default=1)
    passed: Mapped[bool] = mapped_column(Boolean)
    metric_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed: Mapped[dict] = mapped_column(JSON, default=dict)
    bad_sample_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)


class Metric(Base, TenantMixin):
    __tablename__ = "metric"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    table_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    tags: Mapped[dict] = mapped_column(JSON, default=dict)
    # A user may exclude a point they consider wrong (e.g. a double-load spike or a missed
    # load) so it stops skewing the chart and baselines. Their choice — history is kept.
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)


class Incident(Base, TenantMixin, TimestampMixin):
    __tablename__ = "incident"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # Null when the incident came from a compare that is not tied to a monitor.
    monitor_id: Mapped[str | None] = mapped_column(
        ForeignKey("monitor.id"), index=True, nullable=True
    )
    dq_rule_id: Mapped[str | None] = mapped_column(ForeignKey("dq_rule.id"), nullable=True)
    table_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|acknowledged|resolved
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Signal-quality fields: fingerprint for dedup, which pillar/dimension, what kind of
    # signal (rule|anomaly|schema_drift|compare), and the evidence every alert ships with.
    dedup_key: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    dimension: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Triage disposition set by a user when resolving: ok (valid but accepted/expected),
    # false_positive (not a real problem), or confirmed (a real issue). Null = not yet triaged.
    resolution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Silence: suppress alerting for this incident (or a broader scope) until a point in
    # time, with who/why/ticket captured for the audit trail (UI: silence-alert screen).
    silenced_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    silenced_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    silence_scope: Mapped[str | None] = mapped_column(String(20), nullable=True)  # incident|monitor
    silence_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    ticket_url: Mapped[str | None] = mapped_column(String(512), nullable=True)


class IncidentComment(Base, TenantMixin, TimestampMixin):
    """A note a user adds to an incident. Captures who (user_id) and when (ts)."""

    __tablename__ = "incident_comment"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incident.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class LineageEdge(Base, TenantMixin, TimestampMixin):
    """One hop from an upstream table to the table built from it."""

    __tablename__ = "lineage_edge"
    __table_args__ = (
        UniqueConstraint("group_id", "upstream_ref", "downstream_ref", name="uq_lineage_edge"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    upstream_ref: Mapped[str] = mapped_column(String(512))
    downstream_ref: Mapped[str] = mapped_column(String(512))


# ----------------------------- data testing -----------------------------
class CompareJob(Base, TenantMixin, TimestampMixin):
    __tablename__ = "compare_job"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # A batch groups many per-table compare jobs run together (continue-on-error).
    batch_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    batch_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Organization: which project this test case belongs to + grouping taxonomy.
    project_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    source_system: Mapped[str | None] = mapped_column(String(128), nullable=True)
    layer: Mapped[str | None] = mapped_column(String(32), nullable=True)  # bronze|silver|gold|…
    tags: Mapped[list] = mapped_column(JSON, default=list)
    # Optional recurring schedule (5-field cron). None = manual-only. The background scheduler
    # re-runs this test case (or, for a batch pair, its whole batch) when the schedule comes due.
    schedule_cron: Mapped[str | None] = mapped_column(String(128), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))  # count | schema | data
    source: Mapped[dict] = mapped_column(JSON, default=dict)
    target: Mapped[dict] = mapped_column(JSON, default=dict)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    # Outcome of KEY detection on the most recent run — so a test case can be submitted without a
    # key and the UI can show "keys detected in last run on <date>" (or "not detected").
    #   user         — a key was supplied in options
    #   detected     — the engine detected a unique key at run time
    #   not_detected — no unique key could be found (data compare couldn't line up rows)
    key_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    detected_key: Mapped[list] = mapped_column(JSON, default=list)
    key_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Who triggered the most recent execution (may differ from the creator).
    last_run_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Timestamped execution trace of the most recent run (phases, engine, timing, warnings,
    # outcome) — viewable/downloadable from the UI, and deleted with the job.
    execution_log: Mapped[list] = mapped_column(JSON, default=list)
    # Out-of-process executor bookkeeping (subprocess mode): the DB row IS the queue.
    variables: Mapped[dict] = mapped_column(JSON, default=dict)  # runtime {{vars}} for async run
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Live progress for long runs (e.g. streaming large tables): {pct, processed, total}. Cleared
    # when the run finishes. Lets the UI show "42% — 4.2M/10M rows" while a big compare is in flight.
    progress: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class CompareResult(Base, TimestampMixin):
    __tablename__ = "compare_result"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    compare_job_id: Mapped[str] = mapped_column(ForeignKey("compare_job.id"), index=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    detected_key: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    detail_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Resolved runtime variable values used for this execution (so a user can see
    # exactly which variables produced this result).
    variables: Mapped[dict] = mapped_column(JSON, default=dict)


class CompareRun(Base, TenantMixin, TimestampMixin):
    """One EXECUTION of a compare test case — a point-in-time snapshot kept for history so the
    Executions view can show every run, not just the latest. CompareJob/CompareResult hold the
    LATEST state (for the test-case view); these accumulate (one row per run) and are deleted
    with their job."""

    __tablename__ = "compare_run"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    compare_job_id: Mapped[str] = mapped_column(ForeignKey("compare_job.id"), index=True)
    # Denormalized so project-scoped history + grouping/display need no extra joins.
    project_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    batch_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    batch_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Ties every pair-run of a single batch execution together into one "batch run".
    batch_run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))  # count | schema | data
    status: Mapped[str] = mapped_column(String(20))  # succeeded | failed | cancelled
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    # Failure-growth trend (docs/design/11-style): the runtime {{variables}} this run used, a stable
    # fingerprint of them (so the trend compares only like-for-like parameter sets), and the
    # normalized count of failed records for this run.
    variables: Mapped[dict] = mapped_column(JSON, default=dict)
    param_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    failed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_log: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


# ----------------------------- reports -----------------------------
class Report(Base, TenantMixin, TimestampMixin):
    """A named, persisted report that aggregates test-case executions / DQ results into a
    shareable artifact (dms-reporting parity). Headline stats live here; the full generated
    document goes to the ResultStore (D9 residency) via `document_ref`."""

    __tablename__ = "report"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255))
    report_type: Mapped[str] = mapped_column(
        String(32)
    )  # data_validation|data_quality|executive|profile
    # A standing report over a project/scope; each generation is an immutable ReportVersion.
    project_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    spec: Mapped[dict] = mapped_column(JSON, default=dict)  # filters (batch_id, ids, since_days…)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|ready|failed
    # Cache of the latest version for list views (full snapshots live in ReportVersion).
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    document_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    latest_version: Mapped[int] = mapped_column(Integer, default=0)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Scheduling + delivery (phase C): cron + channels (email/slack recipients).
    schedule_cron: Mapped[str | None] = mapped_column(String(100), nullable=True)
    delivery: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReportVersion(Base, TimestampMixin):
    """Immutable snapshot of a report at generation time (point-in-time evidence for sharing
    + audit + trends). The full document lives in the ResultStore via `document_ref`."""

    __tablename__ = "report_version"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    report_id: Mapped[str] = mapped_column(ForeignKey("report.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="ready")  # ready|failed
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    document_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


# ----------------------------- table metadata (load-pattern catalog) -----------------------------
class TableMetadata(Base, TenantMixin, TimestampMixin):
    """Per-table load-pattern catalog (the doc's `dwh_table.load_pattern`). It drives every
    pillar (what a valid watermark is, what "volume" means, how freshness is judged).

    Designed to be INJECTED from external systems (a customer's custom ingestion software),
    so it's keyed by `table_ref` (tenant-global FQN), carries provenance in `source`, and has
    an extensible `config` for pattern-specific params. Resolution: this per-table record
    overrides the monitor-level default."""

    __tablename__ = "table_metadata"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    table_ref: Mapped[str] = mapped_column(String(512), index=True)
    # append | cdc | full_snapshot | scd2 | partitioned | reference | unknown
    load_pattern: Mapped[str] = mapped_column(String(32), default="unknown")
    watermark_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    watermark_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    partition_column: Mapped[str | None] = mapped_column(String(255), nullable=True)  # partitioned
    effective_from_column: Mapped[str | None] = mapped_column(String(255), nullable=True)  # scd2
    effective_to_column: Mapped[str | None] = mapped_column(String(255), nullable=True)  # scd2 end/expiry
    current_flag_column: Mapped[str | None] = mapped_column(String(255), nullable=True)  # scd2
    changed_at_column: Mapped[str | None] = mapped_column(String(255), nullable=True)  # cdc
    batch_id_column: Mapped[str | None] = mapped_column(String(255), nullable=True)  # ETL batch id
    business_key: Mapped[list | None] = mapped_column(JSON, nullable=True)  # scd2 natural key column(s)
    freshness_sla_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)  # extensible per-pattern params
    source: Mapped[str] = mapped_column(String(64), default="user")  # user|auto|<external system>
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)  # for auto-classified
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class DetectionRun(Base, TenantMixin, TimestampMixin):
    """A background load-type DETECTION run over a monitor's scope. Results stream in per table
    (the UI polls `progress` + `results`) so detecting 100+ tables is progressive, not a long hang.
    docs/design/11 — bulk detection UX."""

    __tablename__ = "detection_run"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # Detection is a connection+scope operation (works BEFORE a monitor exists — e.g. the create
    # flow). monitor_id is set when it's run from an existing monitor.
    monitor_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    connection_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    scope: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | done | error
    progress: Mapped[dict] = mapped_column(JSON, default=dict)  # {pct, processed, total}
    results: Mapped[list] = mapped_column(JSON, default=list)   # per-table classification rows
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class MetadataConvention(Base, TenantMixin, TimestampMixin):
    """A named, reusable ETL metadata-column convention (docs/design/11 §3.4). Define a role→column
    map once ("IDMC-CDC", "S3-Loader") and reference it from many connections; a connection whose
    schema is loaded by two tools attaches both, and each table resolves to the best-covering one.
    Shipped presets are served as `sys:<key>` conventions from code; these rows are user-defined."""

    __tablename__ = "metadata_convention"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128))
    roles: Mapped[dict] = mapped_column(JSON, default=dict)  # {role: column}
    scd2: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # optional active-definition knobs
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class Feedback(Base, TenantMixin, TimestampMixin):
    """User-submitted feedback from any page (idea / issue / praise). Admins review it in
    Admin → Feedback. We capture the page + browser context so a report is actionable without
    needing the reporter to describe where they were."""

    __tablename__ = "feedback"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    category: Mapped[str] = mapped_column(String(16), default="idea")  # idea | issue | praise | other
    message: Mapped[str] = mapped_column(Text)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)  # optional 1..5
    page: Mapped[str | None] = mapped_column(String(512), nullable=True)  # route path, e.g. /observability
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)  # full href at submit time
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # viewport, referrer, appVersion, …
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)  # new | reviewed | closed


# ----------------------------- catalog & audit -----------------------------
class CatalogTable(Base, TenantMixin, TimestampMixin):
    __tablename__ = "catalog_table"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connection.id"), index=True)
    database: Mapped[str] = mapped_column(String(255))
    schema_name: Mapped[str] = mapped_column("schema", String(255))
    table_name: Mapped[str] = mapped_column(String(255), index=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    column_meta: Mapped[list] = mapped_column(JSON, default=list)
    is_monitored: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(String(36), index=True)
    group_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


# ----------------------------- integrations (outbound connectivity) -----------------------------
class Integration(Base, TenantMixin, TimestampMixin):
    """A tenant-configured outbound connection: a notification channel (email/slack/teams/
    webhook) or a ticketing target (jira/rally). Each group owns its own — credentials live in
    the SecretStore (secret_ref), never here."""

    __tablename__ = "integration"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    category: Mapped[str] = mapped_column(String(20))  # channel | ticketing
    provider: Mapped[str] = mapped_column(String(32))  # email|slack|teams|webhook|jira|rally
    name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)  # non-secret settings
    secret_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="unconfigured")  # active|error|unconfigured
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class NotificationRoute(Base, TenantMixin, TimestampMixin):
    """Routing rule: when `event` fires at >= `min_severity`, deliver via `integration_id`.
    Per-tenant, so each group routes its own events to its own channels."""

    __tablename__ = "notification_route"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event: Mapped[str] = mapped_column(String(50), default="incident_opened")
    min_severity: Mapped[str] = mapped_column(String(20), default="warning")  # critical|warning|info
    integration_id: Mapped[str] = mapped_column(ForeignKey("integration.id"), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)  # provider-specific mapping (future)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class NotificationDelivery(Base, TenantMixin, TimestampMixin):
    """One delivery attempt — history + dedup (one per integration+event+entity). Keeps the
    rendered payload for the audit trail; no secrets."""

    __tablename__ = "notification_delivery"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    integration_id: Mapped[str] = mapped_column(ForeignKey("integration.id"), index=True)
    event: Mapped[str] = mapped_column(String(50))
    entity_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20))  # sent | failed | skipped
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


# ----------------------------- historical-baseline metrics -----------------------------
class MetricDefinition(Base, TenantMixin, TimestampMixin):
    """A named numeric value tracked over time from a source (SQL / file / dashboard). May be
    attached to a monitor (observability) or a project (validation), or stand alone — the rules
    and evaluation are identical regardless of attachment or source."""

    __tablename__ = "metric_definition"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(20))  # sql | file | dashboard
    source_config: Mapped[dict] = mapped_column(JSON, default=dict)
    connection_id: Mapped[str | None] = mapped_column(ForeignKey("connection.id"), nullable=True)
    schedule_cron: Mapped[str | None] = mapped_column(String(100), nullable=True)  # null = manual
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    retention_days: Mapped[int] = mapped_column(Integer, default=400)  # FR-H6 (year-over-year)
    fallback_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)  # FR-B4
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # FR-B6
    monitor_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class MetricRule(Base, TenantMixin, TimestampMixin):
    """A historical-baseline rule on a metric (deviation/monotonic/growth/…). A metric can have
    many; its overall result is the worst across them (FR-R1)."""

    __tablename__ = "metric_rule"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    metric_def_id: Mapped[str] = mapped_column(ForeignKey("metric_definition.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(32))  # deviation | monotonic | growth | …
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    severity: Mapped[str] = mapped_column(String(20), default="warn")  # warn | fail
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Provenance label — see DQRule.origin. system|suggested|user|injected.
    origin: Mapped[str] = mapped_column(String(16), default="user")
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class MetricObservation(Base, TenantMixin, TimestampMixin):
    """One captured value of a metric at a point in time (FR-H1). Value is Decimal (NFR-5).
    Dedup keyed by (metric_def_id, run_id, dimension_key) in the service (FR-H5)."""

    __tablename__ = "metric_observation"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    metric_def_id: Mapped[str] = mapped_column(ForeignKey("metric_definition.id"), index=True)
    dimension_key: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    value: Mapped[object | None] = mapped_column(Numeric(38, 6), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    business_date: Mapped[object | None] = mapped_column(Date, nullable=True)  # data as-of
    run_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pass")  # pass|warn|fail|learning|error
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)  # FR-H3
    exclude_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)  # FR-B5
    source_ref: Mapped[dict] = mapped_column(JSON, default=dict)  # query hash / file / screenshot
    detail: Mapped[dict] = mapped_column(JSON, default=dict)  # per-rule results (baseline, dev, msg)


# ----------------------------- org administration (settings & service identities) -----------------
class OrgSetting(Base, TenantMixin, TimestampMixin):
    """A namespaced JSON settings blob for the tenant — org identity, auth/session policy, and
    governance policies (alert-silence, deletion & audit). One row per (group_id, key). Secrets
    are never stored here (golden rule: only secret_ref pointers live in the DB)."""

    __tablename__ = "org_setting"
    __table_args__ = (UniqueConstraint("group_id", "key", name="uq_org_setting_group_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    key: Mapped[str] = mapped_column(String(64), index=True)  # organization|auth|governance
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class ServiceAccount(Base, TenantMixin, TimestampMixin):
    """A non-human identity (service account) with an API key. We store only the key's prefix
    (for display) and a salted hash — never the secret itself. The plaintext key is returned
    exactly once, at creation/rotation time (UI: people & access / org settings screens)."""

    __tablename__ = "service_account"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="viewer")
    key_prefix: Mapped[str] = mapped_column(String(16), index=True)  # e.g. "dvp_ab12cd"
    key_hash: Mapped[str] = mapped_column(String(128))  # sha256 hex of the full key
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


# ----------------------------- find column (value locator) index -----------------------------
class FinderColumnProfile(Base, TenantMixin, TimestampMixin):
    """Per-column approximation profile for Find Column. The Bloom filter answers
    "is this value possibly in this column?" so a search can shortlist candidate columns
    without scanning the source; a live query then confirms + samples. Blooms are stored
    inline (LargeBinary) — portable across SQLite/Postgres, no object store needed here."""

    __tablename__ = "finder_column_profile"
    # Find Column is PER-USER: the index is private to the user who built it (unlike connections,
    # test cases and observability, which are group-level). created_by is part of the key so two
    # users can each index the same column independently, and searches are filtered to the caller.
    __table_args__ = (
        UniqueConstraint(
            "group_id", "created_by", "connection_id", "schema_name", "table_name", "column_name",
            name="uq_finder_profile",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_by: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connection.id"), index=True)
    schema_name: Mapped[str] = mapped_column(String(255))
    table_name: Mapped[str] = mapped_column(String(512))
    column_name: Mapped[str] = mapped_column(String(255))
    source_data_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    null_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_val: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_val: Mapped[str | None] = mapped_column(Text, nullable=True)
    # --- shape metadata: cheap gates that prune a column BEFORE the Bloom is ever loaded ---
    inferred_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # integer|decimal|date|string
    is_numeric: Mapped[bool] = mapped_column(Boolean, default=False)
    is_date: Mapped[bool] = mapped_column(Boolean, default=False)
    num_min: Mapped[str | None] = mapped_column(Text, nullable=True)  # Decimal as text (exact)
    num_max: Mapped[str | None] = mapped_column(Text, nullable=True)
    min_len: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_len: Mapped[int | None] = mapped_column(Integer, nullable=True)
    charset_mask: Mapped[int] = mapped_column(Integer, default=0)  # bitmask of char classes present
    pattern_masks: Mapped[list | None] = mapped_column(JSON, nullable=True)  # distinct format masks
    pattern_exhaustive: Mapped[bool] = mapped_column(Boolean, default=False)  # captured ALL masks?
    distinct_est: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_low_card: Mapped[bool] = mapped_column(Boolean, default=False)
    distinct_values: Mapped[list | None] = mapped_column(JSON, nullable=True)  # exact set if low-card
    bloom: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    bloom_m: Mapped[int] = mapped_column(Integer, default=0)
    bloom_k: Mapped[int] = mapped_column(Integer, default=0)
    bloom_bits_set: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # High-water mark of the watermark column at last scan (for incremental merges).
    watermark_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FinderScanConfig(Base, TenantMixin, TimestampMixin):
    """How/when a connection's Find Column index refreshes. One config per connection
    (an optional `schema_name` narrows it). `frequency` 'daily' is picked up by the
    scheduler (never more than once a day); 'manual' only runs on explicit trigger."""

    __tablename__ = "finder_scan_config"
    __table_args__ = (
        UniqueConstraint("group_id", "created_by", "connection_id", name="uq_finder_scan_config"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_by: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connection.id"), index=True)
    schema_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tables: Mapped[list | None] = mapped_column(JSON, nullable=True)
    watermark_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    frequency: Mapped[str] = mapped_column(String(32), default="daily")  # daily | manual
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)


class FinderScanRun(Base, TenantMixin, TimestampMixin):
    """One index build/refresh execution — for status + history in the UI."""

    __tablename__ = "finder_scan_run"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_by: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connection.id"), index=True)
    config_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    scan_type: Mapped[str] = mapped_column(String(16), default="full")  # full | incremental
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | ok | error
    tables_indexed: Mapped[int] = mapped_column(Integer, default=0)
    columns_indexed: Mapped[int] = mapped_column(Integer, default=0)
    rows_scanned: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FinderSearch(Base, TenantMixin, TimestampMixin):
    """One Find Column search, persisted so a user sees their recent searches — the value(s)
    looked for, the scope, how much was scanned (tables/columns/connections), how many rows
    matched, and how long it took — and can re-open or delete them. PER-USER like the rest of
    Find Column (filtered to created_by): a search can reveal which values a user looked for, so
    it's private to that user, never shared across the group. `result` holds the full payload so
    re-opening a past search is instant (no re-scan)."""

    __tablename__ = "finder_search"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_by: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    values: Mapped[list] = mapped_column(JSON)
    match_mode: Mapped[str] = mapped_column(String(16), default="any")  # any | co_occurrence
    scope_label: Mapped[str | None] = mapped_column(String(512), nullable=True)
    connection_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    schemas: Mapped[list | None] = mapped_column(JSON, nullable=True)
    scanned_connections: Mapped[int] = mapped_column(Integer, default=0)
    scanned_tables: Mapped[int] = mapped_column(Integer, default=0)
    scanned_columns: Mapped[int] = mapped_column(Integer, default=0)
    matched_tables: Mapped[int] = mapped_column(Integer, default=0)
    total_matches: Mapped[int] = mapped_column(Integer, default=0)
    took_ms: Mapped[float] = mapped_column(Float, default=0.0)
    used_index: Mapped[bool] = mapped_column(Boolean, default=False)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # full payload for re-open


# ----------------------------- catalog listing cache -----------------------------
class CatalogCache(Base, TenantMixin, TimestampMixin):
    """Cached result of a (slow) catalog listing — schemas/tables/databases for a connection.

    Listing a warehouse's schemas/tables can take many seconds; we cache the result per
    connection + key so pickers (and the Find Column table scan) load instantly, with an
    explicit refresh to re-fetch. `payload` holds the list verbatim; `fetched_at` drives the
    "cached · <time>" / staleness display."""

    __tablename__ = "catalog_cache"
    __table_args__ = (
        UniqueConstraint("group_id", "connection_id", "cache_key", name="uq_catalog_cache"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connection.id"), index=True)
    cache_key: Mapped[str] = mapped_column(String(512), index=True)  # e.g. "tables:DB:SCHEMA"
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
