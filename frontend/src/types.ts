export type Health = "healthy" | "warning" | "critical" | "unknown";

export interface Warning {
  code: string;
  message: string;
  details?: Record<string, unknown> | null;
}

export interface Connection {
  id: string;
  source_type: string;
  name: string;
  coordinates: Record<string, unknown>;
  auth_method: string;
  has_secret: boolean;
  status: string;
  metadata_convention_ids?: string[];
}

/** Find Column: a single (value, column) match inside a table. */
export interface FindColumnMatch {
  value: string;
  column: string;
  matches: number;
}

/** Find Column: one table that contains the searched value(s), with confirmed sample rows. */
export interface FindColumnHit {
  connection_id: string;
  connection_name: string;
  database?: string | null;
  schema: string;
  table: string;
  match_count: number | null;
  columns: FindColumnMatch[];
  sample: { columns: string[]; rows: unknown[][] };
}

export interface FindColumnStats {
  took_ms: number;
  shortlist_ms: number;
  confirm_ms: number;
  blooms_loaded: number;
  candidate_tables: number;
  tables_confirmed: number;
  scanned_columns: number;
}

export interface FindColumnResult {
  match_mode: "any" | "co_occurrence";
  values: string[];
  scanned_connections: number;
  scanned_tables: number;
  matched_tables: number;
  truncated: boolean;
  used_index: boolean;
  stats?: FindColumnStats;
  hits: FindColumnHit[];
  search_id?: string | null;
}

/** One persisted Find Column search (history row) — metadata only; `result` is fetched on open. */
export interface FinderSearchSummary {
  id: string;
  values: string[];
  match_mode: "any" | "co_occurrence";
  scope_label: string | null;
  scanned_connections: number;
  scanned_tables: number;
  scanned_columns: number;
  matched_tables: number;
  total_matches: number;
  took_ms: number;
  used_index: boolean;
  truncated: boolean;
  created_at: string | null;
}

/** Find Column index coverage for a connection. */
export interface FindColumnIndexStatus {
  connection_id: string;
  indexed: boolean;
  columns_indexed: number;
  tables_indexed: number;
  last_scanned_at: string | null;
  last_run: {
    status: string;
    scan_type: string;
    tables_indexed: number;
    columns_indexed: number;
    rows_scanned: number;
    error: string | null;
  } | null;
  config: {
    frequency: string;
    enabled: boolean;
    watermark_column: string | null;
    next_run_at: string | null;
  } | null;
}

/** One table's row in a bulk load-type detection run. */
export interface DetectionResult {
  table_ref: string;
  load_type?: string;
  load_pattern: string;
  confidence?: number;
  reason?: string;
  matched_convention?: { id: string | null; name: string } | null;
  coverage?: { missing_roles?: string[]; present_roles?: string[] };
  suggested_metadata?: Record<string, unknown>;
}

/** A background bulk load-type detection run (poll for streaming results + progress). */
export interface DetectionRun {
  id: string;
  monitor_id?: string | null;
  connection_id?: string | null;
  status: "running" | "done" | "error";
  progress: { pct: number; processed: number; total: number };
  results: DetectionResult[];
  warnings: { code?: string; message: string }[];
  error?: string | null;
}

/** Failure-growth over time for a test case, scoped to one parameter set. */
export interface FailureTrend {
  job_id: string;
  param_hash: string | null;
  variables?: Record<string, unknown>;
  current: number | null;
  previous: number | null;
  abs_delta: number | null;
  pct_change: number | null;
  direction: "up" | "down" | "flat" | "none";
  points: { ts: string | null; failed_count: number | null; status: string }[];
}

/** A named, reusable ETL metadata-column convention (role→column). `system` = shipped preset. */
export interface MetadataConvention {
  id: string;
  name: string;
  roles: Record<string, string>;
  scd2?: Record<string, unknown> | null;
  system: boolean;
}

/** In-app feedback submitted from any page; reviewed by admins in Admin → Feedback. */
export interface Feedback {
  id: string;
  user_id: string | null;
  category: "idea" | "issue" | "praise" | "other";
  message: string;
  rating: number | null;
  page: string | null;
  url: string | null;
  user_agent: string | null;
  context: Record<string, unknown>;
  status: "new" | "reviewed" | "closed";
  created_at: string | null;
}

export interface AuditEntry {
  id: string;
  action: string;
  entity_type: string | null;
  entity_id: string | null;
  detail: Record<string, unknown>;
  ts: string | null;
  user_id: string | null;
  actor: string | null;
  actor_email: string | null;
}

export interface Member {
  user_id: string;
  membership_id: string;
  email: string | null;
  display_name: string | null;
  role: string;
  ldap_managed: boolean;
  joined_at: string | null;
}

// ---- Monitors dashboard summary (health KPIs + status chips) ----
export interface MonitorSummary {
  total: number;
  health: { healthy: number; warning: number; error: number; paused: number };
  by_status: Record<string, number>;
  incidents: { open: number; acknowledged: number; active_total: number; silenced: number };
  monitors_with_active_incidents: number;
  coverage_pct: number;
}

// ---- People & access: RBAC matrix + service identities ----
export interface RoleMatrix {
  permissions: string[];
  roles: { role: string; permissions: string[]; matrix: Record<string, boolean> }[];
}

export interface ServiceAccount {
  id: string;
  name: string;
  role: string;
  key_prefix: string;
  active: boolean;
  last_used_at: string | null;
  created_by: string | null;
  created_at: string | null;
  api_key?: string; // returned exactly once, on create/rotate
}

// ---- Namespaced org settings blob (organization / auth / governance) ----
export interface OrgSetting {
  key: string;
  value: Record<string, unknown>;
  updated_at: string | null;
  updated_by: string | null;
}

export interface SystemInfo {
  version: string;
  profile: string;
  is_local: boolean;
  identity_provider: string;
  secret_backend: string;
  result_backend: string;
  database: string;
  scheduler: { enabled: boolean; interval_seconds: number };
}

// ---- Integrations (outbound channels + routing) ----
export interface Integration {
  id: string;
  category: string;
  provider: string;
  name: string;
  enabled: boolean;
  config: Record<string, unknown>;
  has_secret: boolean;
  status: string;
  last_delivery_at: string | null;
  last_error: string | null;
}

export interface IntegrationFieldSpec {
  key: string;
  label: string;
  input: "text" | "number" | "password";
  secret: boolean;
}

export interface IntegrationProviderSpec {
  provider: string;
  category: string;
  label: string;
  fields: IntegrationFieldSpec[];
}

export interface IntegrationSpecs {
  providers: IntegrationProviderSpec[];
  events: string[];
  severities: string[];
}

export interface NotificationRoute {
  id: string;
  event: string;
  min_severity: string;
  integration_id: string;
  enabled: boolean;
}

// ---- Historical-baseline metrics ----
export interface RuleSuggestionParam {
  key: string;
  label: string;
  default: number;
}
export interface RuleSuggestion {
  key: string;
  type: string;
  label: string;
  rationale: string;
  severity: string;
  config: Record<string, unknown>;
  params: RuleSuggestionParam[];
}

export interface MetricRule {
  id: string;
  metric_def_id: string;
  name: string;
  type: string;
  config: Record<string, unknown>;
  severity: string;
  enabled: boolean;
}

export interface Metric {
  id: string;
  name: string;
  description: string | null;
  source_type: string;
  source_config: Record<string, unknown>;
  connection_id: string | null;
  schedule_cron: string | null;
  last_run_at: string | null;
  reset_at: string | null;
  fallback_threshold: number | null;
  monitor_id: string | null;
  project_id: string | null;
  rules?: MetricRule[];
}

export interface MetricObservationRuleResult {
  rule_id?: string;
  name?: string;
  type?: string;
  status: string;
  baseline_value?: number | null;
  deviation_pct?: number | null;
  limit?: string | null;
  message?: string;
}
export interface MetricObservation {
  id: string;
  dimension_key: string | null;
  value: number | null;
  captured_at: string | null;
  business_date: string | null;
  run_id: string | null;
  status: string;
  excluded: boolean;
  exclude_reason: string | null;
  accepted: boolean;
  detail: { rules?: MetricObservationRuleResult[] };
}

export interface MetricRunResult {
  metric_id: string;
  run_id: string | null;
  observations: {
    dimension_key: string | null;
    value: number | null;
    status: string;
    rules: MetricObservationRuleResult[];
  }[];
}

export interface Monitor {
  id: string;
  connection_id: string;
  name: string;
  description: string | null;
  tags: string[];
  scope: { database?: string; schema?: string; tables?: string[] };
  schedule_cron: string | null;
  schedule_label?: string;
  next_run?: string | null;
  last_run_at?: string | null;
  created_at?: string | null;
  status: string;
  watermark_column: string | null;
  watermark_type: string | null;
  load_type?: string | null;
  lookback_days: number;
  alert_policy?: AlertPolicy;
  project_id?: string | null;
  source_system?: string | null;
  layer?: string | null;
  metadata_convention_ids?: string[] | null;
}

export type FreshnessSource = "auto" | "watermark" | "file_arrival";

export interface AlertPolicy {
  freshness: {
    source: FreshnessSource;
    cadence_hours: number;
    warn_after_hours: number;
    severe_after_hours: number;
  };
  quality: { warn_below: number; severe_below: number };
  volume: { warn_pct: number | null; severe_pct: number | null };
}

export const DEFAULT_ALERT_POLICY: AlertPolicy = {
  freshness: { source: "auto", cadence_hours: 24, warn_after_hours: 24, severe_after_hours: 72 },
  quality: { warn_below: 80, severe_below: 60 },
  volume: { warn_pct: null, severe_pct: null },
};

export interface WatermarkSuggestion {
  column: string;
  type: string;
  confidence: number;
  present_in: number;
  total_tables: number;
  covers_all_tables: boolean;
  nullable_in_any: boolean;
  reason: string;
}

export interface WatermarkDetect {
  connection_id: string;
  tables_inspected: string[];
  suggested: WatermarkSuggestion | null;
  candidates: WatermarkSuggestion[];
}

export interface TableWatermarkCheck {
  table: string;
  exists: boolean;
  actual_type: string | null;
  canonical_type: string | null;
  is_temporal: boolean;
  type_matches: boolean;
  nullable: boolean;
  ok: boolean;
  issues: string[];
  warnings: string[];
}

export interface WatermarkValidation {
  column: string;
  expected_type: string | null;
  valid: boolean;
  tables_total: number;
  tables_with_column: number;
  tables_ok: number;
  tables_with_warnings?: number;
  missing_in: string[];
  tables: TableWatermarkCheck[];
}

// ---- Load-pattern catalog (per-table "how is this table loaded") ----
export interface PatternFieldSpec {
  label: string;
  input: "column" | "columns" | "enum" | "number" | "text" | "bool";
  help?: string;
  options?: string[];
}

export interface PatternSpec {
  pattern: string;
  label: string;
  description: string;
  fields: string[];
}

export interface TableColumn {
  name: string;
  data_type: string;
  nullable: boolean;
}

/** Per-table load configuration captured at creation (mirrors TableMetadata). */
export interface TableLoadConfig {
  load_pattern: string;
  watermark_column?: string;
  watermark_type?: string;
  changed_at_column?: string;
  effective_from_column?: string;
  effective_to_column?: string;
  current_flag_column?: string;
  partition_column?: string;
  batch_id_column?: string;
  business_key?: string[];
  active_mode?: string;
  high_date_value?: string;
  open_end_is_null?: boolean;
  current_flag_value?: string;
  freshness_sla_seconds?: number | null;
}

export interface PillarCard {
  pillar: "freshness" | "volume" | "schema" | "quality";
  status: Health;
  headline: string;
  detail: string | null;
  value: number | null;
  configured: boolean;
  last_load_at?: string | null;
  drift_count?: number;
}

export interface TablePillars {
  monitor_id: string;
  table_ref: string;
  health: Health;
  score: number;
  last_profiled_at: string | null;
  query_shape?: string | null;
  queries?: string[];
  watermark_column: string | null;
  suggested_watermark?: { column: string; type: string } | null;
  load_type?: string;
  load_pattern?: string;
  load_pattern_source?: string;
  pattern_meta?: Record<string, unknown>;
  pillars: PillarCard[];
  lineage: {
    status: string;
    nodes: { name: string; type: string }[];
    upstream?: { name: string; hops: number }[];
    downstream?: { name: string; hops: number }[];
  };
}

export interface TableSummary {
  table_ref: string;
  health: Health;
  score: number;
  pillars: Record<string, Health>;
  scan_count: number;
  last_scanned: string | null;
  load_type?: string;
  volume_note?: string | null;
  freshness_note?: string | null;
  schema_note?: string | null;
  quality_note?: string | null;
}

export interface HistoryPoint {
  id?: string; // metric row id — lets a user exclude this exact point from the chart/baseline
  ts: string;
  value: number;
  baseline?: number | null;
  lo?: number | null;
  hi?: number | null;
  anomaly?: boolean;
  direction?: string;
  score?: number;
}

export interface ColumnStat {
  column: string;
  data_type?: string | null;
  null_rate: number | null;
  distinct_ratio: number | null;
  distinct_approx?: boolean; // true when distinct_ratio came from an approximate (HLL) count
  attention?: boolean; // true when this column has a signal worth a look (e.g. a null-rate spike)
  attention_kind?: string | null; // null_spike | cardinality_drift | … (drives the marker color)
  attention_reason?: string | null;
}

export interface RuleLatest {
  passed: boolean;
  metric_value: number | null;
  threshold: number | null;
  ts: string;
  failing_keys?: Record<string, unknown>[];
  sample_sql?: string | null;
}

export interface Rule {
  id: string;
  monitor_id: string;
  name: string;
  description: string | null;
  type: string;
  table_ref: string | null;
  columns: string[];
  config: Record<string, unknown>;
  dimension: string;
  severity: string;
  scope: string;
  state: string;
  origin?: string;  // system | suggested | user | injected — provenance label
  confidence: number | null;
  version: number;
  latest?: RuleLatest | null;
}

export interface RulePoint {
  ts: string;
  value: number | null;
  passed: boolean;
  threshold: number | null;
}

// ---- Data Validation (compare) ----
export interface CompareSide {
  connection_id?: string;
  database?: string;
  schema?: string;
  table: string;
  filter?: string;
}

export interface PoolStats {
  total_memory_gb: number;
  used_memory_gb: number;
  total_vcpus: number;
  used_vcpus: number;
  waiting_for_resources: number;
}

export interface ActiveJob {
  id: string;
  name: string;
  status: "running" | "queued";
  kind: string;
  project_id?: string | null;
  batch_id?: string | null;
  batch_name?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  last_run_by?: string | null;
  last_run_by_name?: string | null;
}

/** One execution in the run history (a snapshot — the Executions tab shows every run). */
export interface RunRecord {
  id: string; // the CompareRun id (for its log)
  job_id: string; // the test case id (for linking to latest results)
  name?: string | null;
  kind: string;
  status: string; // succeeded | failed | cancelled
  match?: boolean | null;
  summary?: Record<string, number>;
  started_at?: string | null;
  finished_at?: string | null;
  last_run_by?: string | null;
  last_run_by_name?: string | null;
  has_log?: boolean;
}

export interface RunEntry {
  type: "single" | "batch";
  id: string; // run id (single) or batch_run_id (batch)
  when?: string | null;
  last_run_by?: string | null;
  last_run_by_name?: string | null;
  // single:
  run?: RunRecord;
  // batch:
  batch_id?: string | null;
  name?: string | null;
  kind?: string;
  status?: string;
  total?: number;
  succeeded?: number;
  failed?: number;
  matched?: number;
  differed?: number;
  runs?: RunRecord[];
}

export interface CompareBatch {
  batch_id: string;
  name: string;
  kind: string;
  status: string;
  project_id?: string | null;
  total: number;
  succeeded: number;
  failed: number;
  matched: number;
  differed?: number;
  running?: number;
  created_by?: string | null;
  created_by_name?: string | null;
  last_run_by?: string | null;
  last_run_by_name?: string | null;
  created_at?: string | null;
  finished_at?: string | null;
  schedule_cron?: string | null;
  next_run?: string | null;
  jobs?: CompareJob[];
}

export interface CompareJob {
  id: string;
  name: string | null;
  batch_id?: string | null;
  batch_name?: string | null;
  kind: "count" | "schema" | "data";
  status: string;
  // Run-time key detection: 'user' (supplied), 'detected', 'not_detected', or null (never run).
  key_status?: "user" | "detected" | "not_detected" | null;
  detected_key?: string[];
  key_detected_at?: string | null;
  error: string | null;
  error_hint?: string | null;
  has_log?: boolean;
  schedule_cron?: string | null;
  next_run?: string | null;
  progress?: { pct: number; processed: number; total: number } | null;
  project_id?: string | null;
  source: CompareSide;
  target: CompareSide;
  options: Record<string, unknown>;
  created_by?: string | null;
  created_by_name?: string | null;
  last_run_by?: string | null;
  last_run_by_name?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  updated_at?: string | null;
  summary?: Record<string, unknown> | null; // from list
  result?: {
    summary: Record<string, unknown>;
    detected_key: string[];
    warnings: { code: string; message: string }[];
    detail_ref: string | null;
    sample_location?: string | null;
    variables?: Record<string, string>;
  } | null;
}

export interface SampleStore {
  backend: "local" | "snowflake";
  connection_id: string | null;
  connection_name: string | null;
  database: string | null;
  schema: string | null;
  table: string | null;
  location: string | null;
}

export interface CellDiff {
  column: string;
  src: unknown;
  dst: unknown;
  canonical_type?: string;
  differs?: boolean;
  compared?: boolean;  // false = captured for context only, NOT part of the compare
}

export type Bucket = "match" | "value_difference" | "source_only" | "target_only" | "ambiguous";

export interface DiffRow {
  key: Record<string, unknown>;
  bucket: Bucket;
  status?: string;
  cells?: CellDiff[];
  record?: Record<string, unknown>;
  src_count?: number;
  dst_count?: number;
}

export interface Incident {
  id: string;
  monitor_id: string | null;
  dq_rule_id: string | null;
  table_ref: string | null;
  severity: Health | string;
  status: "open" | "acknowledged" | "resolved";
  summary: string | null;
  dimension: string | null;
  kind: string | null;
  evidence: Record<string, unknown>;
  occurrences: number;
  created_at: string | null;
  updated_at: string | null;
  resolved_at: string | null;
  resolution?: string | null;
  resolved_by?: string | null;
  silenced?: boolean;
  silenced_until?: string | null;
  silenced_by?: string | null;
  silence_scope?: string | null;
  silence_note?: string | null;
  ticket_url?: string | null;
}

export interface IncidentComment {
  id: string;
  incident_id: string;
  body: string;
  user_id: string | null;
  author: string | null;
  author_email: string | null;
  ts: string | null;
}

export interface SchemaRec {
  column: string;
  src_type: string | null;
  dst_type: string | null;
  canonical_src: string | null;
  canonical_dst: string | null;
  canonical_match: boolean;
  nullability_match: boolean;
  status: string;
}

export interface ColumnMappingSuggestion {
  source: string;
  target: string;
  method: string;
  confidence: number;
  src_type?: string | null;
  dst_type?: string | null;
  type_match?: boolean;
}

export interface MappingSuggestion {
  source_columns: string[];
  target_columns: string[];
  source_key: string[];
  target_key: string[];
  key_method?: string; // primary_key | detected | none
  key_uniqueness_pct: number;
  mappings: ColumnMappingSuggestion[];
  unmapped_source: string[];
  unmapped_target: string[];
  warnings: { code: string; message: string }[];
}

export interface Tag {
  id: string;
  name: string;
  color: string | null;
  description: string | null;
  usage?: number;
  created_at?: string | null;
}

export interface Project {
  id: string;
  name: string;
  description: string | null;
  tags: string[];
  /** Project-level {{variables}} inherited by every test case; a job/run-time value with the same
   *  name overrides the project one. */
  variables?: Record<string, string>;
  compare_count?: number;
  monitor_count?: number;
  created_at?: string | null;
  archived?: boolean;
  archived_at?: string | null;
  health?: { passing: number; differences: number; failed: number; running: number; not_run: number; pass_rate: number };
}

export interface ProjectTestCase {
  id: string;
  name: string | null;
  kind?: string;
  status?: string;
  source_system: string | null;
  layer: string | null;
  tags: string[];
}

export interface Report {
  id: string;
  name: string;
  report_type: string;
  project_id: string | null;
  spec: Record<string, unknown>;
  status: string;
  summary: Record<string, unknown>;
  latest_version: number;
  schedule_cron: string | null;
  generated_at: string | null;
  created_at: string | null;
  error: string | null;
  document?: Record<string, unknown> | null;
}

export interface ReportVersion {
  id: string;
  report_id: string;
  version: number;
  status: string;
  summary: Record<string, unknown>;
  generated_at: string | null;
  error: string | null;
  document?: Record<string, unknown> | null;
}

export interface ReportTrend {
  points: { version: number; pass_rate_pct: number | null; generated_at: string | null }[];
  delta: number | null;
}
