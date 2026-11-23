import type {
  ActiveJob,
  PoolStats,
  RunEntry,
  AlertPolicy,
  ColumnStat,
  AuditEntry,
  CompareBatch,
  CompareJob,
  Connection,
  HistoryPoint,
  Incident,
  IncidentComment,
  MappingSuggestion,
  Integration,
  IntegrationSpecs,
  DetectionRun,
  FindColumnResult,
  FindColumnIndexStatus,
  FinderSearchSummary,
  FailureTrend,
  Feedback,
  Member,
  MetadataConvention,
  Metric,
  MetricObservation,
  MetricRule,
  MetricRunResult,
  Monitor,
  MonitorSummary,
  OrgSetting,
  RoleMatrix,
  ServiceAccount,
  NotificationRoute,
  PatternSpec,
  PatternFieldSpec,
  Project,
  ProjectTestCase,
  RuleSuggestion,
  SampleStore,
  SystemInfo,
  Report,
  ReportTrend,
  ReportVersion,
  Tag,
  Rule,
  RulePoint,
  DiffRow,
  SchemaRec,
  TablePillars,
  TableSummary,
  WatermarkDetect,
  WatermarkValidation,
  Warning,
} from "./types";

const BASE = "/api/v1";

export interface ApiResult<T> {
  data: T;
  warnings: Warning[];
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<ApiResult<T>> {
  const res = await fetch(BASE + path, {
    method,
    headers: body !== undefined ? { "content-type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  const json = text ? JSON.parse(text) : {};
  if (!res.ok) {
    throw new ApiError(json.detail || json.title || `HTTP ${res.status}`, res.status);
  }
  return { data: json.data as T, warnings: (json.warnings as Warning[]) || [] };
}

// encode a path segment but keep dots (table_ref uses dots)
const seg = (s: string) => encodeURIComponent(s);

export interface AuditQuery {
  entityType?: string;
  entityId?: string;
  action?: string;
  userId?: string;
  since?: string;
  until?: string;
  limit?: number;
}

function auditQuery(opts?: AuditQuery): URLSearchParams {
  const p = new URLSearchParams();
  if (opts?.entityType) p.set("entity_type", opts.entityType);
  if (opts?.entityId) p.set("entity_id", opts.entityId);
  if (opts?.action) p.set("action", opts.action);
  if (opts?.userId) p.set("user_id", opts.userId);
  if (opts?.since) p.set("since", opts.since);
  if (opts?.until) p.set("until", opts.until);
  if (opts?.limit) p.set("limit", String(opts.limit));
  return p;
}

export const api = {
  // per-user UI preferences (last project, last section, …)
  getPreferences: () => call<Record<string, unknown>>("GET", "/me/preferences"),
  savePreferences: (patch: Record<string, unknown>) =>
    call<Record<string, unknown>>("PUT", "/me/preferences", patch),

  // connections
  listConnections: () => call<Connection[]>("GET", "/connections"),
  getConnection: (id: string) => call<Connection>("GET", `/connections/${seg(id)}`),
  createConnection: (body: Record<string, unknown>) => call<Connection>("POST", "/connections", body),
  testConnection: (id: string) =>
    call<{ ok: boolean; detail: string; category: string }>("POST", `/connections/${seg(id)}:test`),
  deleteConnection: (id: string) => call<unknown>("DELETE", `/connections/${seg(id)}`),
  listDatabases: (connectionId: string) =>
    call<{ databases: string[] }>("GET", `/connections/${seg(connectionId)}/databases`),
  listSchemas: (connectionId: string, database?: string, refresh?: boolean) => {
    const p = new URLSearchParams();
    if (database) p.set("database", database);
    if (refresh) p.set("refresh", "true");
    const qs = p.toString();
    return call<{ schemas: string[]; cached?: boolean; cached_at?: string | null }>(
      "GET",
      `/connections/${seg(connectionId)}/schemas${qs ? `?${qs}` : ""}`
    );
  },
  // Clear every cached catalog listing for a connection and re-fetch schemas (slow → fresh).
  refreshCatalogCache: (connectionId: string) =>
    call<{ schemas: string[]; cached_at?: string | null; cleared?: number }>(
      "POST",
      `/connections/${seg(connectionId)}/catalog:refresh`
    ),
  listCatalogTables: (connectionId: string, schema: string, database?: string) =>
    call<{ tables: { name: string; type: string }[] }>(
      "GET",
      `/connections/${seg(connectionId)}/tables?schema=${encodeURIComponent(schema)}${
        database ? `&database=${encodeURIComponent(database)}` : ""
      }`
    ),
  listColumns: (connectionId: string, schema: string, table: string, database?: string) =>
    call<{ columns: { name: string; data_type: string; nullable: boolean }[] }>(
      "GET",
      `/connections/${seg(connectionId)}/columns?schema=${encodeURIComponent(
        schema
      )}&table=${encodeURIComponent(table)}${
        database ? `&database=${encodeURIComponent(database)}` : ""
      }`
    ),
  // approximate row count (warehouse metadata estimate when available)
  rowCount: (connectionId: string, schema: string, table: string, database?: string) =>
    call<{ count: number | null; approx: boolean }>(
      "GET",
      `/connections/${seg(connectionId)}/rowcount?schema=${encodeURIComponent(
        schema
      )}&table=${encodeURIComponent(table)}${database ? `&database=${encodeURIComponent(database)}` : ""}`
    ),
  // a few preview rows from a table/view
  sampleRows: (connectionId: string, schema: string, table: string, limit = 20, database?: string) =>
    call<{ columns: string[]; rows: unknown[][] }>(
      "GET",
      `/connections/${seg(connectionId)}/sample?schema=${encodeURIComponent(
        schema
      )}&table=${encodeURIComponent(table)}&limit=${limit}${
        database ? `&database=${encodeURIComponent(database)}` : ""
      }`
    ),

  // Find Column: locate where value(s) live across a connection's tables, with sample rows.
  findColumn: (body: {
    connection_id?: string;
    connection_ids?: string[];
    values: string[];
    schema?: string;
    schemas?: string[];
    database?: string;
    tables?: string[];
    match_mode?: "any" | "co_occurrence";
    sample_limit?: number;
    max_tables?: number;
    use_index?: "auto" | "never";
    confirm_concurrency?: number;
  }) => call<FindColumnResult>("POST", "/finder/search", body),
  // Find Column Bloom index — build/refresh, coverage status, and refresh schedule.
  buildFinderIndex: (body: {
    connection_id: string;
    schema?: string;
    tables?: string[];
    watermark_column?: string;
    incremental?: boolean;
  }) => call<{ tables_indexed?: number; columns_indexed?: number; rows_scanned?: number; status?: string }>(
    "POST",
    "/finder/index:build",
    body,
  ),
  finderIndexStatus: (connectionId: string) =>
    call<FindColumnIndexStatus>("GET", `/finder/index/status?connection_id=${seg(connectionId)}`),
  setFinderIndexConfig: (body: {
    connection_id: string;
    schema?: string;
    watermark_column?: string;
    frequency?: "daily" | "manual";
    enabled?: boolean;
  }) => call<{ frequency: string; enabled: boolean; next_run_at: string | null }>(
    "PUT",
    "/finder/index/config",
    body,
  ),
  // Find Column search history (per-user): list, re-open (with full result), delete.
  finderSearches: (limit = 50) =>
    call<FinderSearchSummary[]>("GET", `/finder/searches?limit=${limit}`),
  finderSearch: (id: string) =>
    call<FinderSearchSummary & { result: FindColumnResult }>("GET", `/finder/searches/${seg(id)}`),
  deleteFinderSearch: (id: string) =>
    call<{ deleted: string }>("DELETE", `/finder/searches/${seg(id)}`),

  // validate an arbitrary SQL dataset — runs it capped, returns columns + sample (or a warning)
  previewSql: (connectionId: string, sql: string, limit = 20, database?: string) =>
    call<{ ok: boolean; columns: string[]; rows: unknown[][]; row_count: number }>(
      "POST",
      `/connections/${seg(connectionId)}/sql-preview`,
      { sql, limit, ...(database ? { database } : {}) }
    ),

  // load-pattern catalog + per-table metadata
  patternSpecs: () =>
    call<{ patterns: PatternSpec[]; fields: Record<string, PatternFieldSpec> }>(
      "GET",
      "/table-metadata/pattern-specs"
    ),
  classifyPattern: (columns: { name: string; data_type: string }[], rowCount?: number | null) =>
    call<Record<string, unknown>>("POST", "/table-metadata/classify", {
      columns,
      row_count: rowCount ?? null,
    }),
  ruleSuggestions: (loadPattern: string) =>
    call<RuleSuggestion[]>("GET", `/table-metadata/rule-suggestions?load_pattern=${encodeURIComponent(loadPattern)}`),

  // historical-baseline metrics
  listMetrics: (opts?: { monitorId?: string; projectId?: string }) => {
    const p = new URLSearchParams();
    if (opts?.monitorId) p.set("monitor_id", opts.monitorId);
    if (opts?.projectId) p.set("project_id", opts.projectId);
    const qs = p.toString();
    return call<Metric[]>("GET", `/metrics${qs ? `?${qs}` : ""}`);
  },
  getMetric: (id: string) => call<Metric>("GET", `/metrics/${seg(id)}`),
  deleteMetric: (id: string) => call<unknown>("DELETE", `/metrics/${seg(id)}`),
  runMetric: (id: string, body?: { run_id?: string; run_date?: string }) =>
    call<MetricRunResult>("POST", `/metrics/${seg(id)}:run`, body ?? {}),
  metricObservations: (id: string, dimensionKey?: string) =>
    call<MetricObservation[]>(
      "GET",
      `/metrics/${seg(id)}/observations${dimensionKey ? `?dimension_key=${encodeURIComponent(dimensionKey)}` : ""}`,
    ),
  addMetricRule: (id: string, body: { name: string; type: string; config: Record<string, unknown>; severity?: string }) =>
    call<MetricRule>("POST", `/metrics/${seg(id)}/rules`, body),
  deleteMetricRule: (ruleId: string) => call<unknown>("DELETE", `/metrics/rules/${seg(ruleId)}`),
  excludeObservation: (obsId: string, reason: string) =>
    call<MetricObservation>("POST", `/metrics/observations/${seg(obsId)}:exclude`, { reason }),
  acceptObservation: (obsId: string) =>
    call<MetricObservation>("POST", `/metrics/observations/${seg(obsId)}:accept`),
  resetMetric: (id: string, resetAt: string, reason?: string) =>
    call<Metric>("POST", `/metrics/${seg(id)}:reset`, { reset_at: resetAt, reason }),
  acknowledgeTableSuggestions: (body: {
    monitor_id: string;
    table_ref: string;
    connection_id: string;
    load_pattern: string;
    selections: { key: string; params: Record<string, number>; severity?: string }[];
  }) => call<{ metric: Metric; rules: MetricRule[] }>("POST", "/metrics/from-table-suggestions", body),

  // monitors
  listMonitors: () => call<Monitor[]>("GET", "/monitors"),
  monitorsSummary: () => call<MonitorSummary>("GET", "/monitors:summary"),
  getMonitor: (id: string) => call<Monitor>("GET", `/monitors/${seg(id)}`),
  createMonitor: (body: Record<string, unknown>) => call<Monitor>("POST", "/monitors", body),
  updateMonitor: (id: string, body: { name?: string; description?: string; schedule?: string; schedule_cron?: string | null; lookback_days?: number; alert_policy?: AlertPolicy }) =>
    call<Monitor>("PATCH", `/monitors/${seg(id)}`, body),
  deleteMonitor: (id: string) => call<{ id: string; status: string; name: string }>("DELETE", `/monitors/${seg(id)}`),
  listAudit: (entityType: string, entityId?: string) =>
    call<AuditEntry[]>(
      "GET",
      `/audit?entity_type=${encodeURIComponent(entityType)}${
        entityId ? `&entity_id=${seg(entityId)}` : ""
      }`,
    ),
  setWatermark: (id: string, watermark_column: string | null, watermark_type: string | null) =>
    call<Monitor>("POST", `/monitors/${seg(id)}:watermark`, { watermark_column, watermark_type }),
  addLineageEdges: (edges: { upstream: string; downstream: string }[]) =>
    call<{ added: number; submitted: number }>("POST", "/lineage/edges", { edges }),

  // watermark
  detectWatermark: (connectionId: string, scope: object) =>
    call<WatermarkDetect>("POST", `/connections/${seg(connectionId)}/watermark:detect`, { scope }),
  validateWatermark: (connectionId: string, scope: object, column: string, expected_type?: string) =>
    call<WatermarkValidation>("POST", `/connections/${seg(connectionId)}/watermark:validate`, {
      scope,
      column,
      expected_type,
    }),

  // observability
  listTables: (monitorId: string) =>
    call<TableSummary[]>("GET", `/monitors/${seg(monitorId)}/tables`),
  tablePillars: (monitorId: string, tableRef: string) =>
    call<TablePillars>("GET", `/monitors/${seg(monitorId)}/tables/${seg(tableRef)}/pillars`),
  pillarHistory: (monitorId: string, tableRef: string, pillar: string, sinceDays?: number) =>
    call<{
      points: HistoryPoint[];
      metric: string;
      pillar: string;
      load_type?: string;
      anomaly_count?: number;
      baseline?: { median: number | null; lo: number | null; hi: number | null };
    }>(
      "GET",
      `/monitors/${seg(monitorId)}/tables/${seg(tableRef)}/pillars/${pillar}/history${
        sinceDays ? `?since_days=${sinceDays}` : ""
      }`
    ),
  tableColumns: (monitorId: string, tableRef: string) =>
    call<ColumnStat[]>("GET", `/monitors/${seg(monitorId)}/tables/${seg(tableRef)}/columns`),
  columnHistory: (monitorId: string, tableRef: string, column: string) =>
    call<{ column: string; metrics: Record<string, HistoryPoint[]> }>(
      "GET",
      `/monitors/${seg(monitorId)}/tables/${seg(tableRef)}/columns/${seg(column)}/history`
    ),
  profileTable: (monitorId: string, tableRef: string) =>
    call<Record<string, unknown>>("POST", `/monitors/${seg(monitorId)}/tables/${seg(tableRef)}:profile`),
  suggestFreshness: (monitorId: string, tableRef: string) =>
    call<{ suggestion: Record<string, unknown> | null; loads_observed: number; enough_history: boolean; message: string | null }>(
      "GET",
      `/monitors/${seg(monitorId)}/tables/${seg(tableRef)}/freshness:suggest`,
    ),

  // Generate the load-aware DQ rule pack for a table from its detected/confirmed load type
  // (SCD2: active-unique, no-overlap, freshness + an active-count baseline metric). origin=system.
  autogenerateLoadAware: (monitorId: string, tableRef: string) =>
    call<{ created: Rule[]; skipped: string[]; metrics: Record<string, unknown>[] }>(
      "POST",
      `/monitors/${seg(monitorId)}/tables/${seg(tableRef)}/rules:autogenerate-load-aware`,
    ),
  // Bulk load-type detection (connection + scope, works before a monitor exists). Start returns a
  // run id; poll getDetectionRun for streaming per-table results + progress.
  startDetection: (connectionId: string, scope: { database?: string; schema?: string; tables: string[] }) =>
    call<DetectionRun>("POST", `/connections/${seg(connectionId)}:detect-load-type-async`, { scope }),
  getDetectionRun: (runId: string) =>
    call<DetectionRun>("GET", `/monitors/load-type-detections/${seg(runId)}`),
  metadataPresets: () =>
    call<{ roles: string[]; presets: Record<string, { label: string; roles: Record<string, string> }> }>(
      "GET",
      "/table-metadata/metadata-presets",
    ),
  // Named ETL metadata-column conventions (the reusable library: shipped `sys:` presets + user's).
  listMetadataConventions: () =>
    call<MetadataConvention[]>("GET", "/metadata-conventions"),
  // In-app feedback: any user submits; admins review.
  submitFeedback: (body: {
    message: string; category: string; rating?: number | null;
    page?: string; url?: string; user_agent?: string; context?: Record<string, unknown>;
  }) => call<Feedback>("POST", "/feedback", body),
  listFeedback: (status?: string) =>
    call<Feedback[]>("GET", `/feedback${status ? `?status=${encodeURIComponent(status)}` : ""}`),
  updateFeedbackStatus: (id: string, status: string) =>
    call<Feedback>("PATCH", `/feedback/${seg(id)}`, { status }),
  createMetadataConvention: (body: { name: string; roles: Record<string, string>; scd2?: Record<string, unknown> | null }) =>
    call<MetadataConvention>("POST", "/metadata-conventions", body),
  updateMetadataConvention: (id: string, body: Record<string, unknown>) =>
    call<MetadataConvention>("PUT", `/metadata-conventions/${seg(id)}`, body),
  deleteMetadataConvention: (id: string) =>
    call<void>("DELETE", `/metadata-conventions/${seg(id)}`),
  // Auto-create a suggested DQ rule pack for selected tables from a SAMPLE of the last batch
  // (no full scan). Rules land as state=suggested, origin=system for accept/remove.
  suggestPack: (monitorId: string, body: { tables?: string[]; sample_size?: number; level?: number; persist?: boolean }) =>
    call<{ created: Rule[]; skipped: string[]; tables_selected: number; persisted: boolean }>(
      "POST",
      `/monitors/${seg(monitorId)}/rules:suggest-pack`,
      body,
    ),

  // custom DQ rules + metrics
  tableRules: (monitorId: string, tableRef: string) =>
    call<Rule[]>("GET", `/monitors/${seg(monitorId)}/tables/${seg(tableRef)}/rules`),
  createRule: (monitorId: string, body: Record<string, unknown>) =>
    call<Rule>("POST", `/monitors/${seg(monitorId)}/rules`, body),
  // Generate suggested rules from a sample (latest watermark batch, else N rows); nothing saved.
  suggestRulesFromSample: (monitorId: string, body: { level: number; sample_size?: number }) =>
    call<{ level: number; sample_size: number; sampled: number; watermark_used: boolean; table_ref: string; rules: Record<string, unknown>[] }>(
      "POST", `/monitors/${seg(monitorId)}/rules:suggest-from-sample`, body),
  // Test rule specs against the sample without persisting — pass/fail + failing examples.
  dryRunRules: (monitorId: string, body: { rules: Record<string, unknown>[]; sample_size?: number }) =>
    call<{ sampled: number; watermark_used: boolean; table_ref: string; results: { name: string; type: string; applicable: boolean; passed?: boolean; measured?: unknown; failing?: Record<string, unknown>[]; note?: string }[] }>(
      "POST", `/monitors/${seg(monitorId)}/rules:dry-run`, body),
  recordRuleResult: (monitorId: string, ruleId: string, body: Record<string, unknown>) =>
    call<Record<string, unknown>>("POST", `/monitors/${seg(monitorId)}/rules/${seg(ruleId)}:record`, body),
  ruleHistory: (monitorId: string, ruleId: string) =>
    call<{ points: RulePoint[] }>("GET", `/monitors/${seg(monitorId)}/rules/${seg(ruleId)}/history`),

  // pushdown run / evaluate (C2)
  runMonitor: (monitorId: string) =>
    call<{ rules_evaluated: number; tables: { table_ref: string; rules_evaluated: number }[] }>(
      "POST",
      `/monitors/${seg(monitorId)}:run`
    ),
  evaluateRule: (monitorId: string, ruleId: string) =>
    call<Record<string, unknown>>("POST", `/monitors/${seg(monitorId)}/rules/${seg(ruleId)}:evaluate`),
  snapshot: (monitorId: string, tableRef: string, observation: object) =>
    call<Record<string, unknown>>(
      "POST",
      `/monitors/${seg(monitorId)}/tables/${seg(tableRef)}/snapshot`,
      observation
    ),

  // incidents
  listIncidents: (activeOnly = false) =>
    call<Incident[]>("GET", `/incidents${activeOnly ? "?active_only=true" : ""}`),
  tableIncidents: (monitorId: string, tableRef: string) =>
    call<Incident[]>("GET", `/monitors/${seg(monitorId)}/tables/${seg(tableRef)}/incidents`),
  ackIncident: (id: string) => call<Incident>("POST", `/incidents/${seg(id)}:ack`),
  resolveIncident: (id: string, body?: { resolution?: string; note?: string }) =>
    call<Incident>("POST", `/incidents/${seg(id)}:resolve`, body),
  reopenIncident: (id: string) => call<Incident>("POST", `/incidents/${seg(id)}:reopen`),
  silenceIncident: (
    id: string,
    body: { scope?: string; until?: string | null; duration_hours?: number | null; note?: string; ticket_url?: string },
  ) => call<Incident>("POST", `/incidents/${seg(id)}:silence`, body),
  unsilenceIncident: (id: string) => call<Incident>("POST", `/incidents/${seg(id)}:unsilence`),
  incidentComments: (id: string) =>
    call<IncidentComment[]>("GET", `/incidents/${seg(id)}/comments`),
  addIncidentComment: (id: string, body: string) =>
    call<IncidentComment>("POST", `/incidents/${seg(id)}/comments`, { body }),
  excludePoint: (pointId: string, excluded: boolean) =>
    call<{ id: string; excluded: boolean }>("POST", `/observability/points/${seg(pointId)}:exclude`, { excluded }),
  ingestDbtRun: (body: Record<string, unknown>) =>
    call<{
      invocation_id: string | null;
      failed: number;
      passed: number;
      skipped: number;
      opened: number;
      updated: number;
      resolved: number;
    }>("POST", "/dbt/runs", body),

  // data validation (compare)
  listCompares: () => call<CompareJob[]>("GET", "/compares"),
  // in-flight work (running + queued) for the top-bar queue bell
  activeJobs: () =>
    call<{
      jobs: ActiveJob[]; running: number; queued: number; total: number; concurrency: number | null;
      pool?: PoolStats;
    }>("GET", "/compares/active"),
  // Column mapping. mode "fuzzy" (names, instant) or "deep" (samples data, probabilistic).
  // Key detection is opt-in — pass detectKey=true, or call detectKeys.
  suggestMapping: (source: Record<string, unknown>, target: Record<string, unknown>, detectKey = false, mode: "fuzzy" | "deep" = "fuzzy") =>
    call<MappingSuggestion>("POST", "/compares/suggest-mapping", { source, target, detect_key: detectKey, mode }),
  // Detect candidate key columns on demand (samples data — may be slow).
  detectKeys: (source: Record<string, unknown>, target?: Record<string, unknown>) =>
    call<{ source_key: string[]; target_key: string[]; key_method: string; key_uniqueness_pct: number }>(
      "POST", "/compares/detect-keys", { source, target }),
  // Pick ~limit keys from source, then fetch those rows from source AND target (aligned by key).
  sampleByKey: (body: {
    source: Record<string, unknown>; target: Record<string, unknown>;
    key: string[]; key_values?: unknown[][]; columns?: string[];
    column_mappings?: Record<string, string>; limit?: number;
  }) =>
    call<{
      aligned: boolean;
      provided?: boolean;
      warnings: string[];
      key?: string[];
      columns?: { src: string; dst: string }[];
      sampled?: number;
      rows?: { key: Record<string, unknown>; source: Record<string, unknown> | null; target: Record<string, unknown> | null }[];
      source?: { columns: string[]; rows: unknown[][] };
      target?: { columns: string[]; rows: unknown[][] };
    }>("POST", "/compares/sample-by-key", body),
  createCompare: (body: Record<string, unknown>) => call<CompareJob>("POST", "/compares", body),
  // Run the real compare on a small sample (default 1,000 rows) without persisting — a pre-run check.
  dryRun: (body: {
    source: Record<string, unknown>; target: Record<string, unknown>;
    key: string[]; columns?: string[]; column_mappings?: Record<string, string>; limit?: number;
  }) =>
    call<{ sampled: number; summary: Record<string, number>; diffs: unknown[]; warnings: string[] }>(
      "POST", "/compares/dry-run", body),
  // Edit a test case's definition (partial: only the keys sent are changed).
  updateCompare: (jobId: string, body: Record<string, unknown>) =>
    call<CompareJob>("PATCH", `/compares/${seg(jobId)}`, body),
  runCompare: (jobId: string, variables?: Record<string, string>) =>
    call<CompareJob>("POST", `/compares/${seg(jobId)}:run`, variables ? { variables } : undefined),
  // Async rerun: enqueue on the worker tier, return immediately (non-blocking).
  submitCompare: (jobId: string, variables?: Record<string, string>) =>
    call<{ job_id: string; status: string }>("POST", `/compares/${seg(jobId)}:submit`, variables ? { variables } : undefined),
  rerunBatch: (batchId: string) =>
    call<{ batch_id: string; queued: number; status: string }>("POST", `/compares/batches/${seg(batchId)}:rerun`),
  // Set/clear a recurring cron schedule on a test case (for a batch pair, applies to the batch).
  setSchedule: (jobId: string, scheduleCron: string | null) =>
    call<CompareJob>("PUT", `/compares/${seg(jobId)}/schedule`, { schedule_cron: scheduleCron }),
  // Bulk-rerun a project's test cases (default: only failed/differing). Async on the worker tier.
  rerunProject: (projectId: string, onlyFailed = true) =>
    call<{ project_id: string; queued: number; batches: number; test_cases: number }>(
      "POST", `/compares/projects/${seg(projectId)}:rerun?only_failed=${onlyFailed}`),
  // Download URL for a test case's full captured differences as CSV.
  differencesCsvUrl: (jobId: string) => `${BASE}/compares/${seg(jobId)}/differences/export`,
  // Cancel a running/queued execution (stops the engine + aborts the in-flight warehouse query).
  cancelCompare: (jobId: string) => call<CompareJob>("POST", `/compares/${seg(jobId)}:cancel`),
  cancelBatch: (batchId: string) =>
    call<{ batch_id: string; cancelled: number }>("POST", `/compares/batches/${seg(batchId)}:cancel`),
  // Variable names this test case references ({{var}} in table/sql/schema/db/filter/key).
  jobVariables: (jobId: string) => call<string[]>("GET", `/compares/${seg(jobId)}/variables`),
  runBatch: (body: Record<string, unknown>) => call<CompareBatch>("POST", "/compares/batch", body),
  // Async: creates the batch + enqueues every pair on the worker tier, returns immediately
  // (status "queued") so the UI can navigate away while the compares run.
  submitBatch: (body: Record<string, unknown>) =>
    call<{ batch_id: string; queued: number; status: string }>("POST", "/compares/batch:submit", body),
  listBatches: () => call<CompareBatch[]>("GET", "/compares/batches"),
  getBatch: (batchId: string) => call<CompareBatch>("GET", `/compares/batches/${seg(batchId)}`),
  signOffBatch: (batchId: string) => call<Report>("POST", `/compares/batches/${seg(batchId)}:sign-off`),
  getCompare: (jobId: string) => call<CompareJob>("GET", `/compares/${seg(jobId)}`),
  // Failure-growth over time for a test case, scoped to one parameter set (+/- vs previous run).
  failureTrend: (jobId: string, paramHash?: string) =>
    call<FailureTrend>(
      "GET",
      `/compares/${seg(jobId)}/failure-trend${paramHash ? `?param_hash=${encodeURIComponent(paramHash)}` : ""}`,
    ),
  // Execution log (timestamped phases/engine/timing/outcome) + a plain-text download URL.
  compareLogs: (jobId: string) =>
    call<{ ts: string; elapsed_ms: number; level: string; msg: string }[]>("GET", `/compares/${seg(jobId)}/logs`),
  compareLogExportUrl: (jobId: string) => `${BASE}/compares/${seg(jobId)}/logs/export`,
  // Execution HISTORY — every run, newest first (batch pairs grouped). Plus a specific run's log.
  listRuns: (projectId?: string) =>
    call<RunEntry[]>("GET", `/compares/runs${projectId ? `?project_id=${seg(projectId)}` : ""}`),
  runLogs: (runId: string) =>
    call<{ ts: string; elapsed_ms: number; level: string; msg: string }[]>("GET", `/compares/runs/${seg(runId)}/logs`),
  compareDifferences: (jobId: string, bucket?: string) =>
    call<(DiffRow | SchemaRec)[]>(
      "GET",
      `/compares/${seg(jobId)}/differences${bucket ? `?bucket=${bucket}` : ""}`
    ),
  assignCompare: (jobId: string, body: Record<string, unknown>) =>
    call<CompareJob>("PATCH", `/compares/${seg(jobId)}/assignment`, body),
  deleteCompare: (jobId: string) =>
    call<{ deleted: string; artifacts_deleted: number }>("DELETE", `/compares/${seg(jobId)}`),
  deleteBatch: (batchId: string) =>
    call<{ deleted_batch: string; deleted_jobs: number }>("DELETE", `/compares/batches/${seg(batchId)}`),
  // Export URLs (href targets — the browser downloads the attachment directly).
  compareExportUrl: (jobId: string, fmt: string) => `${BASE}/compares/${seg(jobId)}/export?fmt=${fmt}`,
  batchExportUrl: (batchId: string, fmt: string) => `${BASE}/compares/batches/${seg(batchId)}/export?fmt=${fmt}`,

  // identity
  me: () => call<{ user_id: string; org_id: string; group_id: string; role: string; permissions: string[] }>("GET", "/me"),

  // admin — members, system, audit
  systemInfo: () => call<SystemInfo>("GET", "/system/info"),
  sampleStore: () => call<SampleStore>("GET", "/settings/sample-store"),
  setSampleStore: (body: { connection_id: string; database: string; schema: string; table: string }) =>
    call<SampleStore>("PUT", "/settings/sample-store", body),
  clearSampleStore: () => call<SampleStore>("DELETE", "/settings/sample-store"),
  listMembers: (groupId: string) => call<Member[]>("GET", `/groups/${seg(groupId)}/members`),
  addMember: (groupId: string, email: string, role: string) =>
    call<Member>("POST", `/groups/${seg(groupId)}/members`, { email, role }),
  updateMemberRole: (groupId: string, userId: string, role: string) =>
    call<Member>("PUT", `/groups/${seg(groupId)}/members/${seg(userId)}`, { role }),
  removeMember: (groupId: string, userId: string) =>
    call<unknown>("DELETE", `/groups/${seg(groupId)}/members/${seg(userId)}`),
  auditLog: (opts?: AuditQuery) => {
    const qs = auditQuery(opts).toString();
    return call<AuditEntry[]>("GET", `/audit${qs ? `?${qs}` : ""}`);
  },
  auditExportUrl: (opts?: AuditQuery) => {
    const qs = auditQuery(opts).toString();
    return `${BASE}/audit/export${qs ? `?${qs}` : ""}`;
  },

  // admin — people & access (RBAC matrix + service identities)
  roleMatrix: () => call<RoleMatrix>("GET", "/access/roles"),
  listServiceAccounts: () => call<ServiceAccount[]>("GET", "/access/service-accounts"),
  createServiceAccount: (body: { name: string; role: string }) =>
    call<ServiceAccount>("POST", "/access/service-accounts", body),
  rotateServiceAccountKey: (id: string) =>
    call<ServiceAccount>("POST", `/access/service-accounts/${seg(id)}:rotate`),
  deleteServiceAccount: (id: string) => call<unknown>("DELETE", `/access/service-accounts/${seg(id)}`),

  // admin — organization / auth / governance settings (namespaced JSON blobs)
  orgSettings: (key: "organization" | "auth" | "governance") =>
    call<OrgSetting>("GET", `/settings/${key}`),
  setOrgSettings: (key: "organization" | "auth" | "governance", value: Record<string, unknown>) =>
    call<OrgSetting>("PUT", `/settings/${key}`, { value }),
  // admin — worker-pool compute limits (memory GB + vCPUs) + live pool usage
  getCompute: () =>
    call<{ value: { total_memory_gb?: number; max_vcpus?: number }; pool?: PoolStats }>("GET", "/settings/compute"),
  setCompute: (value: { total_memory_gb?: number | null; max_vcpus?: number | null }) =>
    call<{ value: Record<string, unknown>; pool?: PoolStats }>("PUT", "/settings/compute", { value }),

  // admin — integrations (outbound channels + routing)
  integrationSpecs: () => call<IntegrationSpecs>("GET", "/integrations/provider-specs"),
  listIntegrations: () => call<Integration[]>("GET", "/integrations"),
  createIntegration: (body: { provider: string; name: string; values: Record<string, unknown>; enabled?: boolean }) =>
    call<Integration>("POST", "/integrations", body),
  updateIntegration: (id: string, body: Record<string, unknown>) =>
    call<Integration>("PATCH", `/integrations/${seg(id)}`, body),
  deleteIntegration: (id: string) => call<unknown>("DELETE", `/integrations/${seg(id)}`),
  testIntegration: (id: string) => call<{ ok: boolean; detail: string }>("POST", `/integrations/${seg(id)}:test`),
  integrationDeliveries: (id: string, limit = 20) =>
    call<Record<string, unknown>[]>("GET", `/integrations/${seg(id)}/deliveries?limit=${limit}`),
  listRoutes: () => call<NotificationRoute[]>("GET", "/integrations/routes"),
  createRoute: (body: { event?: string; min_severity: string; integration_id: string; enabled?: boolean }) =>
    call<NotificationRoute>("POST", "/integrations/routes", body),
  updateRoute: (id: string, body: { min_severity?: string; enabled?: boolean }) =>
    call<NotificationRoute>("PATCH", `/integrations/routes/${seg(id)}`, body),
  deleteRoute: (id: string) => call<unknown>("DELETE", `/integrations/routes/${seg(id)}`),

  // table load-pattern metadata (injectable from external systems)
  loadPatterns: () => call<string[]>("GET", "/table-metadata/patterns"),
  setTableMetadata: (tableRef: string, body: Record<string, unknown>) =>
    call<Record<string, unknown>>("PUT", `/table-metadata/${seg(tableRef)}`, body),

  // projects
  listProjects: (includeArchived = false) =>
    call<Project[]>("GET", `/projects${includeArchived ? "?include_archived=true" : ""}`),
  createProject: (body: Record<string, unknown>) => call<Project>("POST", "/projects", body),
  getProject: (id: string) => call<Project>("GET", `/projects/${seg(id)}`),
  updateProject: (id: string, body: Record<string, unknown>) =>
    call<Project>("PATCH", `/projects/${seg(id)}`, body),
  deleteProject: (id: string) => call<Project>("DELETE", `/projects/${seg(id)}`),
  restoreProject: (id: string) => call<Project>("POST", `/projects/${seg(id)}:restore`),
  projectTestCases: (id: string) =>
    call<{ compares: ProjectTestCase[]; monitors: ProjectTestCase[] }>(
      "GET",
      `/projects/${seg(id)}/test-cases`
    ),

  // tags (first-class)
  listTags: () => call<Tag[]>("GET", "/tags"),
  createTag: (body: Record<string, unknown>) => call<Tag>("POST", "/tags", body),
  updateTag: (id: string, body: Record<string, unknown>) =>
    call<Tag>("PATCH", `/tags/${seg(id)}`, body),
  deleteTag: (id: string) => call<unknown>("DELETE", `/tags/${seg(id)}`),

  // reports
  listReports: () => call<Report[]>("GET", "/reports"),
  createReport: (body: Record<string, unknown>) => call<Report>("POST", "/reports", body),
  generateReport: (id: string) => call<Report>("POST", `/reports/${seg(id)}:generate`),
  deliverReport: (id: string) => call<{ delivered: { channel: string; recipient: string }[] }>("POST", `/reports/${seg(id)}:deliver`),
  getReport: (id: string) => call<Report>("GET", `/reports/${seg(id)}`),
  deleteReport: (id: string) => call<unknown>("DELETE", `/reports/${seg(id)}`),
  reportVersions: (id: string) => call<ReportVersion[]>("GET", `/reports/${seg(id)}/versions`),
  reportTrend: (id: string) => call<ReportTrend>("GET", `/reports/${seg(id)}/trend`),
  reportExportUrl: (id: string, fmt: string) => `${BASE}/reports/${seg(id)}/export?fmt=${fmt}`,
};
