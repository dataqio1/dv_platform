import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { usePrefs } from "../prefs";
import type { CompareBatch, CompareJob, Connection, TableColumn, TableLoadConfig } from "../types";
import { Button, Field, Modal, TextInput } from "./ui";
import { Combobox, TablePicker } from "./Combobox";
import { TableLoadConfigRow, patternToLoadType, type PatternSpecs } from "./TableLoadConfig";
import { DatasetListPanel, MappingStep, datasetSpec, type ColPair, type Dataset, type Mapping } from "./DatasetMapping";
import { ScheduleControl } from "./ScheduleControl";

// In-progress wizard state is held in sessionStorage so navigating away and back
// (within the session) doesn't lose the form. Cleared on submit or cancel.
const DRAFT_KEY = "dvp.wizardDraft";
type WizardDraft = { s: WizardState; step: number; org: OrgState; vars: Record<string, string> };
function readDraft(): WizardDraft | null {
  try {
    const raw = sessionStorage.getItem(DRAFT_KEY);
    return raw ? (JSON.parse(raw) as WizardDraft) : null;
  } catch {
    return null;
  }
}
function clearDraft() {
  try {
    sessionStorage.removeItem(DRAFT_KEY);
  } catch {
    /* ignore */
  }
}
/** True if a half-finished test case is parked in this session (so the list can reopen it). */
export function hasWizardDraft(): boolean {
  try {
    return !!sessionStorage.getItem(DRAFT_KEY);
  } catch {
    return false;
  }
}

type OrgState = { project: string; source: string; layer: string; tags: string[] };

type KindKey = "counts_compare" | "schema_compare" | "data_validation" | "data_quality";

// How a compare side points at its data. "table" and "file" both resolve to a
// {connection_id, schema, table} spec on the backend — "file" just scopes the
// connection picker to file/S3 sources (whose files are exposed as tables).
type SideMode = "table" | "sql" | "file";
const FILE_SOURCE_TYPES = ["file", "s3"];
const isFileSource = (c?: Connection) => !!c && FILE_SOURCE_TYPES.includes(c.source_type);

// A data-quality test case can bundle up to 10 rules over the same dataset.
const MAX_DQ_RULES = 10;
const DIMENSIONS = ["quality", "freshness", "volume", "schema"];
const SEVERITIES = ["critical", "warning", "info"];

interface DqRule {
  name: string;
  sql: string;
  threshold: string;
  dimension: string;
  severity: string;
}

const newDqRule = (): DqRule => ({
  name: "",
  sql: 'SELECT COUNT(*) AS v FROM {{table}} WHERE "AMOUNT" < 0',
  threshold: "0",
  dimension: "quality",
  severity: "critical",
});

const VAR_RE = /\{\{\s*([a-zA-Z_]\w*)\s*\}\}/g;
function findVars(...parts: string[]): string[] {
  const set = new Set<string>();
  for (const p of parts) {
    let m: RegExpExecArray | null;
    VAR_RE.lastIndex = 0;
    while ((m = VAR_RE.exec(p || ""))) set.add(m[1]);
  }
  return [...set];
}

const KINDS: { key: KindKey; label: string; glyph: string; blurb: string; backend: string }[] = [
  { key: "counts_compare", label: "Counts compare", glyph: "#", blurb: "Do row counts match?", backend: "count" },
  { key: "schema_compare", label: "Schema compare", glyph: "▤", blurb: "Columns & types match?", backend: "schema" },
  { key: "data_validation", label: "Data validation", glyph: "⇄", blurb: "Cell-by-cell differences", backend: "data" },
  { key: "data_quality", label: "Data quality", glyph: "✓", blurb: "Rules on one dataset", backend: "" },
];

type Template = {
  label: string;
  emoji: string;
  kind: KindKey;
  apply: (w: WizardState) => Partial<WizardState>;
};
const TEMPLATES: Template[] = [
  { label: "Migration parity", emoji: "⚡", kind: "data_validation", apply: () => ({ name: "migration parity" }) },
  { label: "Nightly reconciliation", emoji: "🌙", kind: "counts_compare", apply: () => ({ name: "nightly reconciliation", tol: "1" }) },
  { label: "Schema-drift gate", emoji: "🛡", kind: "schema_compare", apply: () => ({ name: "schema drift gate" }) },
  {
    label: "No negative values",
    emoji: "🚫",
    kind: "data_quality",
    apply: () => ({ name: "no negative amount", dqRules: [{ ...newDqRule(), name: "no_negative_amount" }] }),
  },
];

interface WizardState {
  kind: KindKey;
  name: string;
  srcConn: string;
  dstConn: string;
  srcSchema: string;
  dstSchema: string;
  srcDb: string;
  dstDb: string;
  srcMode: SideMode;
  dstMode: SideMode;
  srcTable: string;
  dstTable: string;
  srcSql: string;
  dstSql: string;
  batch: boolean;
  // "Many datasets" mode: independent dataset lists per side + an N:M mapping.
  srcDatasets: Dataset[];
  dstDatasets: Dataset[];
  mappings: Mapping[];
  key: string;
  engine: string;
  memoryGb: string;   // data validation: RAM budget (GB) for in-memory/Spark — "" = auto-detect
  failFast: boolean;  // data validation: stop once the tables clearly differ (default on)
  nullEqualsEmpty: boolean;  // data validation: treat NULL and empty string as equal (default on)
  showExtraColumns: boolean; // data validation: capture non-compared columns for results context (opt-in)
  tol: string;        // counts: tolerance %
  tolRows: string;    // counts: tolerance (absolute rows)
  countDir: string;   // counts: direction (both | source_higher | source_lower)
  maxDiffRows: string; // data validation: allowed differing rows
  maxDiffPct: string;  // data validation: allowed differing %
  schedule: string;    // recurring cron ("" = manual); set in the Configure step
  dqConn: string;
  dqSchema: string;
  dqTable: string;
  dqRules: DqRule[];
  dqLoad: TableLoadConfig;
}

function StepDots({ step, labels, onJump, canJump }: {
  step: number;
  labels: string[];
  onJump: (n: number) => void;
  canJump: (n: number) => boolean;
}) {
  return (
    <div className="mb-5 flex flex-wrap items-center gap-2">
      {labels.map((l, i) => {
        const n = i + 1;
        const active = n === step;
        const done = n < step;
        const clickable = !active && canJump(n);
        return (
          <div key={l} className="flex items-center gap-2">
            <button
              type="button"
              disabled={!clickable}
              onClick={() => clickable && onJump(n)}
              title={clickable ? `Go to “${l}”` : undefined}
              className={`flex items-center gap-2 rounded-lg px-1 py-0.5 ${clickable ? "cursor-pointer hover:bg-slate-50" : "cursor-default"}`}
            >
              <span
                className={`grid h-6 w-6 place-items-center rounded-full text-xs font-semibold ${
                  active ? "bg-teal text-white" : done ? "bg-teal/20 text-teal-700" : "bg-slate-100 text-slate-400"
                }`}
              >
                {done ? "✓" : n}
              </span>
              <span className={`text-xs ${active ? "font-semibold text-navy" : clickable ? "text-slate-500" : "text-slate-400"}`}>{l}</span>
            </button>
            {n < labels.length && <span className="mx-1 h-px w-6 bg-slate-200" />}
          </div>
        );
      })}
    </div>
  );
}

function DataPanel({
  title, connections, conn, setConn, schema, setSchema, db, setDb, mode, setMode, table, setTable, sql, setSql, multi, tableOnly, schemaOnly, matchedTables,
}: {
  title: string; connections: Connection[]; conn: string; setConn: (v: string) => void;
  schema: string; setSchema: (v: string) => void;
  db?: string; setDb?: (v: string) => void;
  mode: SideMode; setMode: (v: SideMode) => void;
  table: string; setTable: (v: string) => void; sql: string; setSql: (v: string) => void;
  multi?: boolean; tableOnly?: boolean; schemaOnly?: boolean; matchedTables?: string[];
}) {
  const showSql = !tableOnly && !schemaOnly && mode === "sql";
  // Database override is optional and only meaningful for a relation (table/schema) side —
  // blank keeps the connection's default. It accepts a literal or a {{var}}.
  const showDb = !showSql && !tableOnly && setDb !== undefined;
  const fileMode = mode === "file";
  const fileConns = connections.filter(isFileSource);
  const [schemas, setSchemas] = useState<string[]>([]);
  const [tables, setTables] = useState<{ name: string; type: string }[]>([]);
  const [schemasLoading, setSchemasLoading] = useState(false);
  const [tablesLoading, setTablesLoading] = useState(false);

  // Entering File mode, point at a file/S3 connection (the current one may be a warehouse).
  useEffect(() => {
    if (!fileMode) return;
    if (!isFileSource(connections.find((c) => c.id === conn)) && fileConns[0]) setConn(fileConns[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileMode]);

  // load schemas when the connection changes (a warehouse query — can take a moment)
  useEffect(() => {
    if (!conn) return;
    setSchemasLoading(true);
    api.listSchemas(conn).then((r) => {
      // Load the available schemas but never auto-pick one — the user chooses (or edit-restore
      // already set the saved schema). Auto-selecting a schema the user didn't choose is wrong.
      setSchemas(r.data.schemas ?? []);
    }).catch(() => setSchemas([])).finally(() => setSchemasLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conn]);

  // load tables when the schema changes
  useEffect(() => {
    if (!conn || !schema || showSql) return;
    setTablesLoading(true);
    api.listCatalogTables(conn, schema).then((r) => setTables(r.data.tables)).catch(() => setTables([]))
      .finally(() => setTablesLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conn, schema, showSql]);

  // Schema/table listings are cached (fast on repeat loads). Refresh clears the cache for this
  // connection and re-fetches from the source — use it when the catalog changed upstream.
  async function refreshCatalog() {
    if (!conn) return;
    setSchemasLoading(true);
    try {
      const r = await api.refreshCatalogCache(conn);
      setSchemas(r.data.schemas ?? []);
    } catch {
      /* leave current schemas */
    } finally {
      setSchemasLoading(false);
    }
    if (schema && !showSql) {
      setTablesLoading(true);
      try {
        const t = await api.listCatalogTables(conn, schema);  // cache was just cleared → live fetch
        setTables(t.data.tables);
      } catch {
        setTables([]);
      } finally {
        setTablesLoading(false);
      }
    }
  }

  const connOpts = (fileMode ? fileConns : connections).map((c) => ({ value: c.id, label: `${c.name} (${c.source_type})` }));
  const tableOpts = tables.map((t) => ({ value: t.name, label: t.type === "view" ? `${t.name}  (view)` : t.name }));
  const selectedTables = table.split(/[,\n]/).map((t) => t.trim()).filter(Boolean);

  return (
    <div className="space-y-2 rounded-xl border border-slate-200 bg-white p-4">
      <div className="flex items-center justify-between">
        <div className="text-xs font-bold uppercase tracking-wide text-slate-500">{title}</div>
        {!tableOnly && !schemaOnly && (
          <div className="flex gap-1">
            {(["table", "sql", "file"] as const).map((m) => (
              <button key={m} onClick={() => setMode(m)}
                className={`rounded px-2 py-0.5 text-[11px] font-medium ${mode === m ? "bg-navy text-white" : "bg-slate-100 text-slate-600"}`}>
                {m === "table" ? "Table / View" : m === "sql" ? "SQL" : "File"}
              </button>
            ))}
          </div>
        )}
      </div>
      {/* every dropdown is a searchable combobox — type to filter or pick */}
      <Combobox value={conn} onChange={setConn} options={connOpts} allowCustom={false}
        placeholder={fileMode ? "file connection" : "connection"} />
      {fileMode && fileConns.length === 0 && (
        <p className="text-[11px] text-amber-600">
          No file connections yet — add one under Connections (source type “File” or “S3”), then pick it here.
        </p>
      )}

      {/* Nothing downstream is shown or loaded until a connection is picked — no default schema,
          no eager warehouse queries. Everything populates only after the user selects a connection. */}
      {!conn ? (
        <p className="px-1 py-1 text-[11px] text-slate-400">Select a connection to choose a {schemaOnly ? "schema" : "schema and table"}.</p>
      ) : (
        <>
      {showDb && (
        <TextInput value={db ?? ""} onChange={(e) => setDb?.(e.target.value)}
          placeholder="database (optional — default from connection; {{var}} ok)" />
      )}

      {!showSql && (
        <div className="flex items-center justify-end gap-2 text-[11px] text-slate-400">
          {(schemasLoading || tablesLoading) && (
            <span className="flex items-center gap-1">
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-300 border-t-teal" />
              loading…
            </span>
          )}
          <button
            onClick={refreshCatalog}
            disabled={schemasLoading || tablesLoading}
            title="Re-fetch schemas & tables from the source (clears the cache)"
            className="font-medium text-teal-700 hover:underline disabled:opacity-50"
          >
            ↻ Refresh
          </button>
        </div>
      )}

      {schemaOnly ? (
        <>
          <Combobox value={schema} onChange={setSchema} options={schemas} loading={schemasLoading} placeholder="select a schema" />
          {/* The target's OWN catalog for the chosen schema. We flag which tables match the
              source selection (compared) and which source tables are missing here. This is the
              target schema's real tables — not a copy of the source selection. */}
          {schema && (
            tablesLoading ? (
              <p className="text-[11px] text-slate-400">Loading tables in {schema}…</p>
            ) : tables.length === 0 ? (
              <p className="text-[11px] text-amber-600">No tables found in {schema}.</p>
            ) : (
              (() => {
                const src = matchedTables ?? [];
                const targetNames = new Set(tables.map((t) => t.name));
                const missing = src.filter((n) => !targetNames.has(n));
                return (
                  <div className="rounded-lg border border-slate-200">
                    <div className="sticky top-0 border-b border-slate-100 bg-slate-50 px-3 py-1.5 text-[11px] text-slate-500">
                      {tables.length} table{tables.length === 1 ? "" : "s"} in <span className="font-medium">{schema}</span>
                      {src.length > 0 && <> · {src.length - missing.length}/{src.length} source table{src.length === 1 ? "" : "s"} matched by name</>}
                    </div>
                    <div className="max-h-56 overflow-auto">
                      {tables.map((t) => {
                        const matched = src.includes(t.name);
                        return (
                          <div key={t.name} className="flex items-center justify-between gap-2 px-3 py-1 text-sm">
                            <span className={matched ? "font-medium text-navy" : "text-slate-600"}>
                              {t.name}
                              {t.type === "view" && <span className="ml-1 rounded bg-slate-100 px-1 text-[10px] text-slate-500">view</span>}
                            </span>
                            {matched && <span className="text-[11px] font-medium text-teal-700">✓ compared</span>}
                          </div>
                        );
                      })}
                    </div>
                    {missing.length > 0 && (
                      <div className="border-t border-amber-100 bg-amber-50 px-3 py-1.5 text-[11px] text-amber-700">
                        ⚠ Not in {schema}: {missing.join(", ")} — these source tables have no same-named target and will be skipped.
                      </div>
                    )}
                  </div>
                );
              })()
            )
          )}
          {matchedTables && (
            <p className="text-[11px] text-slate-400">
              Batch mode — each source table is compared to the <span className="font-medium">same-named</span> table in this schema.
              To compare differently-named tables, switch to “One table”.
            </p>
          )}
        </>
      ) : showSql ? (
        <textarea value={sql} onChange={(e) => setSql(e.target.value)} rows={10}
          placeholder={"SELECT id, amount FROM orders WHERE region = 'EU'"}
          className="w-full resize-y rounded-lg border border-slate-300 px-3 py-2 font-mono text-xs outline-none focus:border-teal" style={{ minHeight: "12rem" }} />
      ) : (
        <>
          {/* Schema first, then tables (DB-specific; both searchable, type or select) */}
          <Combobox value={schema} onChange={setSchema} options={schemas} loading={schemasLoading} placeholder="select a schema" />
          {multi ? (
            <TablePicker
              items={tables}
              value={selectedTables}
              onChange={(names) => setTable(names.join(", "))}
            />
          ) : (
            <Combobox value={table} onChange={setTable} options={tableOpts} loading={tablesLoading}
              placeholder={fileMode ? "select a file" : "select a table or view"} />
          )}
        </>
      )}
        </>
      )}
    </div>
  );
}

export type CompareSeed = {
  incidentId: string;
  connectionId: string;
  database: string;
  schema: string;
  table: string;
  key: string;
};

/** Result of a sample dry-run inside the wizard: a verdict + counts + which columns differ,
 *  computed on ~1,000 sampled rows and never persisted. */
function DryRunSummary({ loading, err, data }: {
  loading: boolean; err: string | null;
  data: { sampled: number; summary: Record<string, number>; diffs: unknown[]; warnings: string[] } | null;
}) {
  if (loading) return (
    <div className="flex flex-col items-center gap-2 px-1 py-10 text-center">
      <span className="h-6 w-6 animate-spin rounded-full border-2 border-slate-300 border-t-teal" />
      <div className="text-xs font-semibold text-navy">Running the compare on a sample…</div>
      <div className="text-[11px] text-slate-400">~1,000 rows from both sides — nothing is saved.</div>
    </div>
  );
  if (err) return <p className="px-1 py-4 text-xs text-rose-600">{err}</p>;
  if (!data) return null;
  if (data.sampled === 0) return <p className="px-1 py-4 text-xs text-slate-500">{data.warnings?.[0] ?? "No sample rows found in the source."}</p>;
  const s = data.summary;
  const matched = s.matched_rows ?? 0;
  const vdiff = s.value_difference_rows ?? s.mismatched_rows ?? 0;
  const srcOnly = s.source_only_rows ?? 0;
  const tgtOnly = s.target_only_rows ?? 0;
  const rate = s.match_rate_pct ?? 0;
  const clean = vdiff === 0 && srcOnly === 0 && tgtOnly === 0;
  const perCol = (s as Record<string, unknown>).per_column_mismatch as Record<string, number> | undefined;
  const cells: [string, number, string][] = [
    ["Matched", matched, "text-emerald-600"], ["Value diff", vdiff, "text-amber-600"],
    ["Source only", srcOnly, "text-sky-600"], ["Target only", tgtOnly, "text-violet-600"],
  ];
  return (
    <div className="space-y-3">
      <div className={`rounded-lg px-3 py-2 text-sm ${clean ? "border border-emerald-300 bg-emerald-50 text-emerald-800" : "border border-amber-300 bg-amber-50 text-amber-800"}`}>
        {clean ? "✓ " : "⚠ "}Dry-run on <span className="font-semibold">{data.sampled.toLocaleString()}</span> sample rows — <span className="font-semibold">{Math.round(rate)}%</span> match.
        {clean ? " The key and mapping line up cleanly — safe to run the full test." : " Differences found on the sample — review the key / mapping / tolerances before the full run."}
      </div>
      {data.warnings?.map((w, i) => <p key={i} className="rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-700">{w}</p>)}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {cells.map(([label, val, tone]) => (
          <div key={label} className="rounded-lg border border-slate-200 p-3 text-center">
            <div className={`text-lg font-bold ${tone}`}>{val.toLocaleString()}</div>
            <div className="text-[11px] text-slate-400">{label}</div>
          </div>
        ))}
      </div>
      {perCol && Object.keys(perCol).length > 0 && (
        <div className="rounded-lg border border-slate-200 p-3">
          <div className="mb-2 text-xs font-semibold text-navy">Which columns differ</div>
          <div className="space-y-1">
            {Object.entries(perCol).sort((a, b) => b[1] - a[1]).map(([col, n]) => (
              <div key={col} className="flex items-center justify-between text-[11px]">
                <span className="font-mono text-slate-700">{col}</span>
                <span className="font-medium text-amber-700">{n.toLocaleString()} rows</span>
              </div>
            ))}
          </div>
        </div>
      )}
      <p className="text-[11px] text-slate-400">This was a bounded sample, not the full table — run the test for the complete result.</p>
    </div>
  );
}

/** A plain columns × rows sample table (single source, no comparison) — used by DQ "View data"
 *  so the user can see the dataset's columns and values while writing rule SQL. */
function DataSample({ loading, err, data }: {
  loading: boolean; err: string | null; data: { columns: string[]; rows: unknown[][] } | null;
}) {
  if (loading) return (
    <div className="flex flex-col items-center gap-2 px-1 py-10 text-center">
      <span className="h-6 w-6 animate-spin rounded-full border-2 border-slate-300 border-t-teal" />
      <div className="text-xs font-semibold text-navy">Fetching sample rows…</div>
      <div className="text-[11px] text-slate-400">Reading from the dataset — this can take a moment on large tables.</div>
    </div>
  );
  if (err) return <p className="px-1 py-4 text-xs text-rose-600">{err}</p>;
  if (!data) return null;
  if (!data.columns.length) return <p className="px-1 py-4 text-xs text-slate-400">No rows returned for this dataset.</p>;
  const cell = (v: unknown) => (v === null || v === undefined ? "∅" : String(v));
  return (
    <div className="space-y-2">
      <p className="text-xs text-slate-500">
        {data.rows.length} sample row{data.rows.length === 1 ? "" : "s"} · {data.columns.length} columns — use these column names in your rule SQL.
      </p>
      <div className="overflow-auto rounded-lg border border-slate-200">
        <table className="w-full text-[11px]">
          <thead className="bg-slate-50 text-left text-[10px] uppercase tracking-wide text-slate-400">
            <tr>{data.columns.map((c) => <th key={c} className="whitespace-nowrap px-2 py-1 font-semibold">{c}</th>)}</tr>
          </thead>
          <tbody>
            {data.rows.map((row, i) => (
              <tr key={i} className="border-t border-slate-100">
                {data.columns.map((_, j) => <td key={j} className="whitespace-nowrap px-2 py-1 font-mono text-slate-700">{cell(row[j])}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Edit mode: reverse a stored single test case back into wizard state ──
const BACKEND_TO_KIND: Record<string, KindKey> = {
  data: "data_validation", schema: "schema_compare", count: "counts_compare",
};
// Option keys the wizard owns (and rebuilds on save). Any OTHER key present on the stored
// options (e.g. timeout_seconds) is carried through unchanged so editing never silently
// drops configuration the wizard doesn't model.
const WIZARD_OPTION_KEYS = new Set([
  "key", "engine", "direction", "tolerance_pct", "tolerance_rows",
  "max_diff_rows", "max_diff_pct", "memory_gb", "fail_fast", "null_equals_empty", "show_extra_columns", "incident_id",
  "columns", "ignore_columns", "column_mappings",
]);

function sideToDataset(side: Record<string, unknown>, id: string): Dataset {
  const sql = typeof side.sql === "string" ? side.sql : "";
  return {
    id,
    connId: (side.connection_id as string) ?? "",
    kind: sql ? "sql" : "table",
    schema: (side.schema as string) ?? undefined,
    db: (side.database as string) ?? undefined,
    name: sql ? "query" : ((side.table as string) ?? ""),
    sql: sql || undefined,
    filter: (side.filter as string) ?? undefined,
  };
}

function columnsFromOptions(opts: Record<string, unknown>): ColPair[] {
  const cols: ColPair[] = [];
  const mappings = (opts.column_mappings as Record<string, string>) ?? {};
  const included = (opts.columns as string[]) ?? [];
  const excluded = (opts.ignore_columns as string[]) ?? [];
  for (const src of included) cols.push({ src, dst: mappings[src] ?? src, include: true });
  for (const [src, dst] of Object.entries(mappings)) {
    if (!cols.some((c) => c.src === src)) cols.push({ src, dst, include: true });
  }
  for (const src of excluded) cols.push({ src, dst: src, include: false });
  return cols;
}

function editStateFromJob(job: CompareJob): Partial<WizardState> {
  const opts = (job.options ?? {}) as Record<string, unknown>;
  const src = sideToDataset(job.source as unknown as Record<string, unknown>, "ds_edit_src");
  const dst = sideToDataset(job.target as unknown as Record<string, unknown>, "ds_edit_dst");
  const cols = columnsFromOptions(opts);
  const key = Array.isArray(opts.key) ? (opts.key as string[]).join(",") : "";
  const mapping: Mapping = {
    srcId: src.id, dstId: dst.id, key, colsLoaded: true,
    columns: cols.length ? cols : undefined,
    direction: (opts.direction as string) ?? undefined,
    tolPct: opts.tolerance_pct !== undefined ? String(opts.tolerance_pct) : undefined,
    tolRows: opts.tolerance_rows !== undefined ? String(opts.tolerance_rows) : undefined,
  };
  return {
    kind: BACKEND_TO_KIND[job.kind] ?? "data_validation",
    name: job.name ?? "",
    srcConn: src.connId, dstConn: dst.connId,
    srcSchema: src.schema ?? "", dstSchema: dst.schema ?? "",
    srcDatasets: [src], dstDatasets: [dst], mappings: [mapping],
    key,
    engine: (opts.engine as string) ?? "auto",
    memoryGb: opts.memory_gb !== undefined ? String(opts.memory_gb) : "",
    failFast: opts.fail_fast === undefined ? true : Boolean(opts.fail_fast),
    nullEqualsEmpty: opts.null_equals_empty === undefined ? true : Boolean(opts.null_equals_empty),
    showExtraColumns: Boolean(opts.show_extra_columns),
    tol: opts.tolerance_pct !== undefined ? String(opts.tolerance_pct) : "0",
    tolRows: opts.tolerance_rows !== undefined ? String(opts.tolerance_rows) : "0",
    countDir: (opts.direction as string) ?? "both",
    maxDiffRows: opts.max_diff_rows !== undefined ? String(opts.max_diff_rows) : "",
    maxDiffPct: opts.max_diff_pct !== undefined ? String(opts.max_diff_pct) : "",
    schedule: job.schedule_cron ?? "",
  };
}

// Reverse a stored batch (its pairs = CompareJobs) back into wizard datasets + mappings. Each
// mapping is tagged with its job id so save can PATCH it in place; added pairs have no jobId
// (→ created) and removed pairs leave a job id behind (→ deleted).
function editStateFromBatch(batch: CompareBatch): Partial<WizardState> {
  const jobs = batch.jobs ?? [];
  const first = (jobs[0]?.options ?? {}) as Record<string, unknown>;
  const srcDatasets: Dataset[] = [];
  const dstDatasets: Dataset[] = [];
  const mappings: Mapping[] = [];
  jobs.forEach((job, i) => {
    const o = (job.options ?? {}) as Record<string, unknown>;
    const a = sideToDataset(job.source as unknown as Record<string, unknown>, `ds_b_src_${i}`);
    const b = sideToDataset(job.target as unknown as Record<string, unknown>, `ds_b_dst_${i}`);
    srcDatasets.push(a);
    dstDatasets.push(b);
    const cols = columnsFromOptions(o);
    mappings.push({
      srcId: a.id, dstId: b.id, jobId: job.id, colsLoaded: true,
      key: Array.isArray(o.key) ? (o.key as string[]).join(",") : "",
      columns: cols.length ? cols : undefined,
      direction: (o.direction as string) ?? undefined,
      tolPct: o.tolerance_pct !== undefined ? String(o.tolerance_pct) : undefined,
      tolRows: o.tolerance_rows !== undefined ? String(o.tolerance_rows) : undefined,
    });
  });
  return {
    kind: BACKEND_TO_KIND[batch.kind || jobs[0]?.kind || "data"] ?? "data_validation",
    name: batch.name,
    srcDatasets, dstDatasets, mappings,
    srcConn: srcDatasets[0]?.connId ?? "",
    dstConn: dstDatasets[0]?.connId ?? "",
    key: Array.isArray(first.key) ? (first.key as string[]).join(",") : "",
    engine: (first.engine as string) ?? "auto",
    memoryGb: first.memory_gb !== undefined ? String(first.memory_gb) : "",
    failFast: first.fail_fast === undefined ? true : Boolean(first.fail_fast),
    nullEqualsEmpty: first.null_equals_empty === undefined ? true : Boolean(first.null_equals_empty),
    showExtraColumns: Boolean(first.show_extra_columns),
    tol: first.tolerance_pct !== undefined ? String(first.tolerance_pct) : "0",
    tolRows: first.tolerance_rows !== undefined ? String(first.tolerance_rows) : "0",
    countDir: (first.direction as string) ?? "both",
    maxDiffRows: first.max_diff_rows !== undefined ? String(first.max_diff_rows) : "",
    maxDiffPct: first.max_diff_pct !== undefined ? String(first.max_diff_pct) : "",
    schedule: batch.schedule_cron ?? "",
  };
}

export function TestCaseWizard({
  connections,
  projectId,
  projectName,
  seed,
  editJob,
  editBatch,
  cloneJob,
  onCancel,
  onNavigate,
}: {
  connections: Connection[];
  projectId?: string;
  projectName?: string;
  seed?: CompareSeed;
  editJob?: CompareJob;
  editBatch?: CompareBatch;
  cloneJob?: CompareJob;
  onCancel: () => void;
  onNavigate: (path: string) => void;
}) {
  const editMode = Boolean(editJob || editBatch);
  const { prefs, setPref } = usePrefs();
  const first = connections[0]?.id ?? "";
  const last = connections[connections.length - 1]?.id ?? first;
  // Restore an in-progress draft if present; else seed connections/schemas from the last
  // test case the user built (requirement: remember last connection/schema used).
  const fromIncident = Boolean(seed?.incidentId);
  // Clone pre-fills from an existing test case; don't let a stale create-draft clobber it.
  const draft = useMemo(() => (fromIncident || cloneJob ? null : readDraft()), [fromIncident, cloneJob]);
  const lw = (prefs.lastWizard as Partial<WizardState> | undefined) ?? {};
  // Edit mode opens on the Source step (kind is already known; Back still reaches step 1).
  const [step, setStep] = useState(editJob || fromIncident ? 2 : (draft?.step ?? 1));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [vars, setVars] = useState<Record<string, string>>(draft?.vars ?? {});
  const [allTags, setAllTags] = useState<{ name: string }[]>([]);
  const [org, setOrg] = useState<OrgState>(draft?.org ?? { project: "", source: "", layer: "", tags: [] });
  useEffect(() => {
    api.listTags().then((r) => setAllTags(r.data)).catch(() => setAllTags([]));
  }, []);
  const defaultState: WizardState = {
    kind: "data_validation",
    name: "",
    // Don't auto-pick a connection the user didn't choose. Remember-last (lw) still pre-fills when
    // present; otherwise leave blank so the user selects — and schemas only load after that choice.
    srcConn: lw.srcConn ?? "",
    dstConn: lw.dstConn ?? "",
    srcSchema: lw.srcSchema ?? "",
    dstSchema: lw.dstSchema ?? "",
    srcDb: "", dstDb: "", srcMode: "table", dstMode: "table",
    srcTable: "", dstTable: "", srcSql: "", dstSql: "",
    batch: false,
    srcDatasets: [], dstDatasets: [], mappings: [],
    key: "", engine: "auto",
    memoryGb: "", failFast: true, nullEqualsEmpty: true, showExtraColumns: false,
    tol: "0", tolRows: "0", countDir: "both", maxDiffRows: "", maxDiffPct: "",
    schedule: "",
    dqConn: lw.srcConn ?? first,
    dqSchema: "", dqTable: "",
    dqRules: [{ ...newDqRule(), name: "no_negative_amount" }],
    dqLoad: { load_pattern: "unknown" },
  };
  const [s, setS] = useState<WizardState>(
    cloneJob
      ? { ...defaultState, ...editStateFromJob(cloneJob), name: cloneJob.name ? `Copy of ${cloneJob.name}` : "" }
      : editBatch
      ? { ...defaultState, ...editStateFromBatch(editBatch) }
      : editJob
      ? { ...defaultState, ...editStateFromJob(editJob) }
      : fromIncident && seed
      ? {
          ...defaultState,
          name: seed.table ? `prove ${seed.table}` : "prove fix",
          srcConn: connections.find((c) => c.id !== seed.connectionId)?.id ?? (seed.connectionId || first),
          dstConn: seed.connectionId || last,
          dstSchema: seed.schema,
          dstDb: seed.database,
          dstTable: seed.table,
          key: seed.key,
          dqConn: seed.connectionId || first,
        }
      : draft?.s
      ? {
          ...defaultState,
          ...draft.s,
          // Default newer fields for drafts saved before they existed — fail-fast is ON by default.
          failFast: draft.s.failFast ?? true,
          nullEqualsEmpty: draft.s.nullEqualsEmpty ?? true,
          showExtraColumns: draft.s.showExtraColumns ?? false,
          memoryGb: draft.s.memoryGb ?? "",
          schedule: draft.s.schedule ?? "",
          dqLoad: draft.s.dqLoad ?? { load_pattern: "unknown" },
          srcDatasets: draft.s.srcDatasets ?? [],
          dstDatasets: draft.s.dstDatasets ?? [],
          mappings: draft.s.mappings ?? [],
        }
      : defaultState
  );
  // Load-pattern catalog + the DQ dataset's columns (for per-table load config).
  const [specs, setSpecs] = useState<PatternSpecs | null>(null);
  const [dqCols, setDqCols] = useState<TableColumn[]>([]);
  const [dqColsLoading, setDqColsLoading] = useState(false);
  const [dqDetecting, setDqDetecting] = useState(false);
  useEffect(() => {
    api.patternSpecs().then((r) => setSpecs(r.data)).catch(() => undefined);
  }, []);
  useEffect(() => {
    if (s.kind !== "data_quality" || !s.dqConn || !s.dqTable) {
      setDqCols([]);
      return;
    }
    let ignore = false;
    setDqColsLoading(true);
    api
      .listColumns(s.dqConn, s.dqSchema, s.dqTable)
      .then((r) => !ignore && setDqCols(r.data.columns ?? []))
      .catch(() => !ignore && setDqCols([]))
      .finally(() => !ignore && setDqColsLoading(false));
    return () => {
      ignore = true;
    };
  }, [s.kind, s.dqConn, s.dqSchema, s.dqTable]);
  // Persist the in-progress form so an accidental navigation doesn't lose it. Editing an existing
  // test case must NOT touch the create-draft (it would pre-fill the next "New test case").
  useEffect(() => {
    if (editMode || cloneJob) return;
    try {
      sessionStorage.setItem(DRAFT_KEY, JSON.stringify({ s, step, org, vars }));
    } catch {
      /* ignore quota/availability errors */
    }
  }, [s, step, org, vars, editMode, cloneJob]);

  // Dry-run: run the real compare on a sample of the first mapped pair, no persistence — lets the
  // user sanity-check the key/mapping before the first full (expensive) run.
  const [dryOpen, setDryOpen] = useState(false);
  const [dryLoading, setDryLoading] = useState(false);
  const [dryErr, setDryErr] = useState<string | null>(null);
  const [dry, setDry] = useState<{ sampled: number; summary: Record<string, number>; diffs: unknown[]; warnings: string[] } | null>(null);
  const dryRunPair = async () => {
    const m = validMappings[0];
    if (!m) return;
    const a = s.srcDatasets.find((d) => d.id === m.srcId);
    const b = s.dstDatasets.find((d) => d.id === m.dstId);
    if (!a || !b) return;
    const keyStr = m.key && m.key.trim() ? m.key : s.key;
    const keyArr = keyStr.split(",").map((k) => k.trim()).filter(Boolean);
    const colsList = m.columns ?? [];
    const included = colsList.filter((c) => c.include && c.dst);
    const column_mappings: Record<string, string> = {};
    for (const c of included) if (c.src !== c.dst) column_mappings[c.src] = c.dst;
    setDryOpen(true); setDryLoading(true); setDryErr(null); setDry(null);
    try {
      const r = await api.dryRun({
        source: datasetSpec(a), target: datasetSpec(b),
        key: keyArr, columns: included.map((c) => c.src), column_mappings, limit: 1000,
      });
      setDry(r.data);
    } catch (e) {
      setDryErr((e as Error).message);
    } finally {
      setDryLoading(false);
    }
  };

  // "View data" for the DQ dataset — a single-source sample (no target) so the user can see the
  // columns & values while writing rule SQL.
  const [dqSample, setDqSample] = useState<{ columns: string[]; rows: unknown[][] } | null>(null);
  const [dqSampleOpen, setDqSampleOpen] = useState(false);
  const [dqSampleLoading, setDqSampleLoading] = useState(false);
  const [dqSampleErr, setDqSampleErr] = useState<string | null>(null);
  const viewDqData = async () => {
    setDqSampleOpen(true); setDqSampleLoading(true); setDqSampleErr(null); setDqSample(null);
    try {
      const r = await api.sampleRows(s.dqConn, s.dqSchema, s.dqTable, 20);
      setDqSample({ columns: r.data.columns ?? [], rows: r.data.rows ?? [] });
    } catch (e) {
      setDqSampleErr((e as Error).message);
    } finally {
      setDqSampleLoading(false);
    }
  };

  const cancel = () => {
    clearDraft();
    onCancel();
  };
  // On a successful create/run: drop the draft and remember the connections/schemas used.
  const finish = (path: string) => {
    clearDraft();
    setPref("lastWizard", {
      srcConn: s.srcConn,
      dstConn: s.dstConn,
      srcSchema: s.srcSchema,
      dstSchema: s.dstSchema,
    });
    onNavigate(path);
  };
  const set = (p: Partial<WizardState>) => setS((prev) => ({ ...prev, ...p }));
  const setRule = (i: number, p: Partial<DqRule>) =>
    setS((prev) => ({ ...prev, dqRules: prev.dqRules.map((r, j) => (j === i ? { ...r, ...p } : r)) }));
  const addRule = () =>
    setS((prev) =>
      prev.dqRules.length >= MAX_DQ_RULES ? prev : { ...prev, dqRules: [...prev.dqRules, newDqRule()] }
    );
  const removeRule = (i: number) =>
    setS((prev) => ({ ...prev, dqRules: prev.dqRules.filter((_, j) => j !== i) }));

  const dqAutoDetect = async () => {
    setDqDetecting(true);
    try {
      const cols = dqCols.length
        ? dqCols
        : await api
            .listColumns(s.dqConn, s.dqSchema, s.dqTable)
            .then((r) => r.data.columns ?? [])
            .catch(() => []);
      const r = await api.classifyPattern(cols.map((c) => ({ name: c.name, data_type: c.data_type })));
      const d = r.data as Record<string, unknown>;
      set({
        dqLoad: {
          load_pattern: (d.load_pattern as string) ?? "unknown",
          watermark_column: d.watermark_column as string | undefined,
          watermark_type: d.watermark_type as string | undefined,
          changed_at_column: d.changed_at_column as string | undefined,
          effective_from_column: d.effective_from_column as string | undefined,
          current_flag_column: d.current_flag_column as string | undefined,
          partition_column: d.partition_column as string | undefined,
          freshness_sla_seconds: (d.freshness_sla_seconds as number | undefined) ?? null,
        },
      });
    } catch {
      /* non-fatal — user can set the pattern manually */
    } finally {
      setDqDetecting(false);
    }
  };

  const kindMeta = KINDS.find((k) => k.key === s.kind)!;
  const isCompare = s.kind !== "data_quality";
  const connName = (id: string) => connections.find((c) => c.id === id)?.name ?? id.slice(0, 8);
  // Runtime variables referenced anywhere in a compare definition.
  const detectedVars = useMemo(
    () => (isCompare ? findVars(s.srcTable, s.dstTable, s.srcSql, s.dstSql, s.srcSchema, s.dstSchema, s.srcDb, s.dstDb, s.key) : []),
    [isCompare, s.srcTable, s.dstTable, s.srcSql, s.dstSql, s.srcSchema, s.dstSchema, s.srcDb, s.dstDb, s.key]
  );
  const varsComplete = detectedVars.every((v) => (vars[v] ?? "").trim());

  const review = useMemo(() => {
    if (s.kind === "data_quality")
      return `Run ${s.dqRules.length} rule${s.dqRules.length === 1 ? "" : "s"} on ${connName(s.dqConn)}.${s.dqTable || "…"}.`;
    const verb = s.kind === "counts_compare" ? "Compare row counts" : s.kind === "schema_compare" ? "Compare columns & types" : "Compare cell values";
    const n = s.mappings.length;
    let extra = "";
    if (s.kind === "counts_compare") {
      const bound = [s.tol && Number(s.tol) ? `${s.tol}%` : "", s.tolRows && Number(s.tolRows) ? `${s.tolRows} rows` : ""].filter(Boolean).join(" / ");
      const dir = s.countDir === "source_higher" ? "source may be higher" : s.countDir === "source_lower" ? "source may be lower" : "within tolerance";
      extra = ` · ${dir}${bound ? ` by ≤ ${bound}` : s.countDir === "both" ? " (exact)" : ""}`;
    } else if (s.kind === "data_validation" && (s.maxDiffRows || s.maxDiffPct)) {
      const bound = [s.maxDiffRows ? `${s.maxDiffRows} rows` : "", s.maxDiffPct ? `${s.maxDiffPct}%` : ""].filter(Boolean).join(" / ");
      extra = ` · allow ≤ ${bound} differing`;
    }
    return `${verb} across ${n} mapped dataset pair${n === 1 ? "" : "s"}${extra}.`;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s]);

  // Dynamic step model: compare kinds are guided (source → target → mapping → run);
  // data quality stays single-dataset (no target step).
  const stepLabels = isCompare
    ? ["What to test", "Source", "Target", "Mapping", "Configure & run"]
    : ["What to test", "Choose data", "Review & run"];
  const lastStep = stepLabels.length;

  // Auto-detect a memory budget from the source size when we reach the Configure step: pull the
  // first source table's approximate row count and bucket it into a GB tier. (Pushdown engines
  // barely use memory; this matters for the in-memory/Spark path on big tables.)
  const [recommendedGb, setRecommendedGb] = useState<number | null>(null);
  useEffect(() => {
    if (step !== lastStep || s.kind !== "data_validation") return;
    const d = s.srcDatasets.find((x) => x.kind === "table" && x.schema);
    if (!d) { setRecommendedGb(null); return; }
    let alive = true;
    api.rowCount(d.connId, d.schema!, d.name, d.db).then((r) => {
      if (!alive) return;
      const rows = r.data.count ?? 0;
      setRecommendedGb(rows < 500_000 ? 2 : rows < 5_000_000 ? 4 : rows < 50_000_000 ? 8 : 16);
    }).catch(() => alive && setRecommendedGb(null));
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, lastStep, s.kind, s.srcDatasets]);

  // A pair is runnable only if both ends resolve and any SQL side actually has a query.
  const dsOk = (d?: Dataset) => !!d && (d.kind !== "sql" || !!d.sql?.trim());
  const validMappings = s.mappings.filter((m) => {
    const a = s.srcDatasets.find((d) => d.id === m.srcId);
    const b = s.dstDatasets.find((d) => d.id === m.dstId);
    return dsOk(a) && dsOk(b);
  });
  const canAdvance = (st: number): boolean => {
    if (!isCompare) {
      if (st === 2)
        return !!s.dqConn && !!s.dqTable && s.dqRules.length > 0 &&
          s.dqRules.every((r) => r.name.trim() && r.sql.trim());
      return true;
    }
    if (st === 2) return s.srcDatasets.length > 0;
    if (st === 3) return s.dstDatasets.length > 0;
    if (st === 4) return validMappings.length > 0;
    return true;
  };
  // Clicking a step in the header jumps straight there: back is always allowed; forward only when
  // every intermediate step is already satisfied (in edit mode everything is pre-filled, so any
  // step is reachable without clicking Next repeatedly).
  const canGoTo = (n: number): boolean => {
    if (n < step) return true;
    for (let k = 1; k < n; k++) if (!canAdvance(k)) return false;
    return true;
  };
  // Dry-run applies to a data-validation compare with a mapped pair and a key to line rows up by.
  const canDry = isCompare && s.kind === "data_validation" && validMappings.length > 0 &&
    Boolean(validMappings[0]?.key?.trim() || s.key.trim());


  const run = async () => {
    setBusy(true);
    setErr(null);
    try {
      const name = s.name || review.slice(0, 60);
      const variables = Object.fromEntries(detectedVars.map((v) => [v, vars[v]]));
      if (isCompare) {
        // Batch-level (default) options; each pair can override key/column mapping.
        const backend = kindMeta.backend; // "data" | "schema" | "count"
        const options: Record<string, unknown> = {};
        if (s.kind === "data_validation" && s.key.trim()) options.key = s.key.split(",").map((k) => k.trim()).filter(Boolean);
        if (seed?.incidentId) options.incident_id = seed.incidentId;
        if (s.kind === "data_validation" && s.engine && s.engine !== "auto") options.engine = s.engine;
        // Counts pass-condition: direction (sign) + bounded tolerance (% and/or rows).
        if (s.kind === "counts_compare") {
          options.direction = s.countDir || "both";
          if (s.tol) options.tolerance_pct = Number(s.tol);
          if (s.tolRows) options.tolerance_rows = Number(s.tolRows);
        }
        // Data validation "allowed differences": pass if within the row/percent budget.
        if (s.kind === "data_validation") {
          if (s.maxDiffRows) options.max_diff_rows = Number(s.maxDiffRows);
          if (s.maxDiffPct) options.max_diff_pct = Number(s.maxDiffPct);
          // Memory budget (GB) for the in-memory/Spark path — guards against OOM on big tables.
          // Explicit choice wins; otherwise use the auto-detected recommendation (if we got one).
          const mem = s.memoryGb ? Number(s.memoryGb) : recommendedGb;
          if (mem) options.memory_gb = mem;
          // Fail-fast is on by default; only send it when the user turned it OFF.
          if (s.failFast === false) options.fail_fast = false;
          // null==empty is on by default; only send it when the user turned it OFF.
          if (s.nullEqualsEmpty === false) options.null_equals_empty = false;
          // capture non-compared columns for results context (opt-in).
          if (s.showExtraColumns) options.show_extra_columns = true;
        }
        const projId = projectId || org.project || null; // test case lives in the active project
        // One compare per mapped pair, each with its own key + (fuzzy, user-adjusted) column
        // mapping, and per-kind column include/exclude.
        const pairs = validMappings.map((m) => {
          const a = s.srcDatasets.find((d) => d.id === m.srcId)!;
          const b = s.dstDatasets.find((d) => d.id === m.dstId)!;
          const pairOpts: Record<string, unknown> = {};
          if (m.key && m.key.trim()) pairOpts.key = m.key.split(",").map((k) => k.trim()).filter(Boolean);
          const colsList = m.columns ?? [];
          if (colsList.length) {
            const included = colsList.filter((c) => c.include && c.dst);
            const excluded = colsList.filter((c) => !c.include);
            const cm: Record<string, string> = {};
            for (const c of included) cm[c.src] = c.dst;
            if (Object.keys(cm).length) pairOpts.column_mappings = cm;
            if (backend === "data" && included.length) pairOpts.columns = included.map((c) => c.src);
            if (backend === "schema" && excluded.length) pairOpts.ignore_columns = excluded.map((c) => c.src);
          }
          // Per-table counts conditions override the batch-level pass condition for this pair.
          if (backend === "count") {
            if (m.direction) pairOpts.direction = m.direction;
            if (m.tolPct !== undefined && m.tolPct !== "") pairOpts.tolerance_pct = Number(m.tolPct);
            if (m.tolRows !== undefined && m.tolRows !== "") pairOpts.tolerance_rows = Number(m.tolRows);
          }
          return { source: datasetSpec(a), target: datasetSpec(b), label: `${a.name} → ${b.name}`, options: pairOpts };
        });
        // Submit asynchronously — compares run on the worker tier so the user isn't held
        // here while (potentially long) Snowflake jobs execute. Land on the Executions tab,
        // where this run shows "Running" and updates as it progresses.
        await api.submitBatch({
          kind: kindMeta.backend, name, project_id: projId,
          source: { connection_id: s.srcDatasets[0]?.connId },
          target: { connection_id: s.dstDatasets[0]?.connId },
          table_pairs: pairs, options, variables,
        });
        finish(projId ? `/validation/projects/${projId}?tab=cases` : `/validation?tab=cases`);
      } else {
        const mon = await api.createMonitor({
          connection_id: s.dqConn, name: `${name} (DQ)`,
          scope: { schema: s.dqSchema || undefined, tables: [s.dqTable] },
          watermark_column: s.dqLoad.watermark_column || null,
          watermark_type: s.dqLoad.watermark_column ? s.dqLoad.watermark_type || "timestamp" : null,
          load_type: patternToLoadType(s.dqLoad.load_pattern),
          project_id: projectId || org.project || null, source_system: org.source || null,
          layer: org.layer || null, tags: org.tags,
        });
        // One test case can bundle many rules over the same dataset.
        for (const r of s.dqRules) {
          await api.createRule(mon.data.id, {
            name: r.name, type: "custom_sql", table_ref: null, severity: r.severity,
            config: { expression: r.sql, threshold: Number(r.threshold), dimension: r.dimension },
          });
        }
        await api.runMonitor(mon.data.id);
        const tbls = await api.listTables(mon.data.id);
        // Persist the per-table load configuration (keyed by the monitor's resolved ref).
        const ref = tbls.data[0]?.table_ref ?? (s.dqSchema ? `${s.dqSchema}.${s.dqTable}` : s.dqTable);
        if (s.dqLoad.load_pattern !== "unknown") {
          const body: Record<string, unknown> = { load_pattern: s.dqLoad.load_pattern, source: "user" };
          for (const k of [
            "watermark_column", "watermark_type", "changed_at_column", "effective_from_column",
            "current_flag_column", "partition_column", "freshness_sla_seconds",
          ] as const) {
            const v = s.dqLoad[k];
            if (v !== undefined && v !== "" && v !== null) body[k] = v;
          }
          await api.setTableMetadata(ref, body).catch(() => undefined);
        }
        finish(`/observability/${mon.data.id}/tables/${encodeURIComponent(ref)}`);
      }
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };

  // Edit mode: update the existing single test case in place (no new run, no duplicate).
  // Rebuilds the definition from the wizard's single source/target/mapping + Configure settings,
  // sets the schedule, and lands back on the project's Executions tab.
  const save = async () => {
    if (!editJob) return;
    setBusy(true);
    setErr(null);
    try {
      const backend = kindMeta.backend; // "data" | "schema" | "count"
      const m = validMappings[0];
      if (!m) throw new Error("Pick a source, target and mapping before saving.");
      const a = s.srcDatasets.find((d) => d.id === m.srcId)!;
      const b = s.dstDatasets.find((d) => d.id === m.dstId)!;
      const opts: Record<string, unknown> = {};
      const keyStr = m.key && m.key.trim() ? m.key : s.key;
      if (s.kind === "data_validation" && keyStr.trim())
        opts.key = keyStr.split(",").map((k) => k.trim()).filter(Boolean);
      if (s.kind === "data_validation" && s.engine && s.engine !== "auto") opts.engine = s.engine;
      if (s.kind === "data_validation") {
        if (s.maxDiffRows) opts.max_diff_rows = Number(s.maxDiffRows);
        if (s.maxDiffPct) opts.max_diff_pct = Number(s.maxDiffPct);
        const mem = s.memoryGb ? Number(s.memoryGb) : recommendedGb;
        if (mem) opts.memory_gb = mem;
        if (s.failFast === false) opts.fail_fast = false;
        if (s.nullEqualsEmpty === false) opts.null_equals_empty = false;
        if (s.showExtraColumns) opts.show_extra_columns = true;
      }
      if (s.kind === "counts_compare") {
        opts.direction = m.direction || s.countDir || "both";
        const tolPct = m.tolPct !== undefined && m.tolPct !== "" ? m.tolPct : s.tol;
        const tolRows = m.tolRows !== undefined && m.tolRows !== "" ? m.tolRows : s.tolRows;
        if (tolPct) opts.tolerance_pct = Number(tolPct);
        if (tolRows) opts.tolerance_rows = Number(tolRows);
      }
      const colsList = m.columns ?? [];
      if (colsList.length) {
        const included = colsList.filter((c) => c.include && c.dst);
        const excluded = colsList.filter((c) => !c.include);
        const cm: Record<string, string> = {};
        for (const c of included) if (c.src !== c.dst) cm[c.src] = c.dst;
        if (Object.keys(cm).length) opts.column_mappings = cm;
        if (backend === "data" && included.length) opts.columns = included.map((c) => c.src);
        if (backend === "schema" && excluded.length) opts.ignore_columns = excluded.map((c) => c.src);
      }
      // Carry through any stored options the wizard doesn't model (e.g. timeout_seconds).
      const original = (editJob.options ?? {}) as Record<string, unknown>;
      const preserved = Object.fromEntries(
        Object.entries(original).filter(([k]) => !WIZARD_OPTION_KEYS.has(k)),
      );
      await api.updateCompare(editJob.id, {
        name: s.name.trim() || editJob.name,
        kind: backend,
        source: datasetSpec(a),
        target: datasetSpec(b),
        options: { ...preserved, ...opts },
      });
      await api.setSchedule(editJob.id, s.schedule.trim() ? s.schedule.trim() : null);
      const projId = editJob.project_id;
      finish(projId ? `/validation/projects/${projId}?tab=cases` : `/validation?tab=cases`);
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };

  // Edit mode (batch): update every pair in place, create pairs the user added, delete pairs they
  // removed, and apply name/schedule across the batch. No new run is started.
  const saveBatch = async () => {
    if (!editBatch) return;
    setBusy(true);
    setErr(null);
    try {
      const backend = kindMeta.backend;
      if (!validMappings.length) throw new Error("A batch needs at least one source → target pair.");
      const name = s.name.trim() || editBatch.name;
      // Batch-level options from the Configure step, merged into every pair (mirrors create).
      const batchOpts: Record<string, unknown> = {};
      if (s.kind === "data_validation" && s.key.trim())
        batchOpts.key = s.key.split(",").map((k) => k.trim()).filter(Boolean);
      if (s.kind === "data_validation" && s.engine && s.engine !== "auto") batchOpts.engine = s.engine;
      if (s.kind === "data_validation") {
        if (s.maxDiffRows) batchOpts.max_diff_rows = Number(s.maxDiffRows);
        if (s.maxDiffPct) batchOpts.max_diff_pct = Number(s.maxDiffPct);
        const mem = s.memoryGb ? Number(s.memoryGb) : recommendedGb;
        if (mem) batchOpts.memory_gb = mem;
        if (s.failFast === false) batchOpts.fail_fast = false;
        if (s.nullEqualsEmpty === false) batchOpts.null_equals_empty = false;
        if (s.showExtraColumns) batchOpts.show_extra_columns = true;
      }
      if (s.kind === "counts_compare") {
        batchOpts.direction = s.countDir || "both";
        if (s.tol) batchOpts.tolerance_pct = Number(s.tol);
        if (s.tolRows) batchOpts.tolerance_rows = Number(s.tolRows);
      }
      const pairOptsFor = (m: Mapping): Record<string, unknown> => {
        const po: Record<string, unknown> = {};
        if (m.key && m.key.trim()) po.key = m.key.split(",").map((k) => k.trim()).filter(Boolean);
        const colsList = m.columns ?? [];
        if (colsList.length) {
          const included = colsList.filter((c) => c.include && c.dst);
          const excluded = colsList.filter((c) => !c.include);
          const cm: Record<string, string> = {};
          for (const c of included) if (c.src !== c.dst) cm[c.src] = c.dst;
          if (Object.keys(cm).length) po.column_mappings = cm;
          if (backend === "data" && included.length) po.columns = included.map((c) => c.src);
          if (backend === "schema" && excluded.length) po.ignore_columns = excluded.map((c) => c.src);
        }
        if (backend === "count") {
          if (m.direction) po.direction = m.direction;
          if (m.tolPct !== undefined && m.tolPct !== "") po.tolerance_pct = Number(m.tolPct);
          if (m.tolRows !== undefined && m.tolRows !== "") po.tolerance_rows = Number(m.tolRows);
        }
        return po;
      };

      const kept = new Set<string>();
      for (const m of validMappings) {
        const a = s.srcDatasets.find((d) => d.id === m.srcId)!;
        const b = s.dstDatasets.find((d) => d.id === m.dstId)!;
        const label = `${a.name} → ${b.name}`;
        const body = {
          kind: backend,
          name: `${name}: ${label}`,
          batch_name: name,
          source: datasetSpec(a),
          target: datasetSpec(b),
          options: { ...batchOpts, ...pairOptsFor(m) },
        };
        if (m.jobId) {
          kept.add(m.jobId);
          await api.updateCompare(m.jobId, body);
        } else {
          await api.createCompare({
            ...body,
            project_id: editBatch.project_id ?? null,
            batch_id: editBatch.batch_id,
          });
        }
      }
      // Delete pairs the user removed.
      for (const job of editBatch.jobs ?? []) {
        if (!kept.has(job.id)) await api.deleteCompare(job.id);
      }
      // Apply the schedule to every current pair of the batch.
      const cron = s.schedule.trim() ? s.schedule.trim() : null;
      const current = await api.getBatch(editBatch.batch_id);
      for (const job of current.data.jobs ?? []) await api.setSchedule(job.id, cron);
      const projId = editBatch.project_id;
      finish(projId ? `/validation/projects/${projId}?tab=cases` : `/validation?tab=cases`);
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <StepDots step={step} labels={stepLabels} onJump={setStep} canJump={canGoTo} />
      {seed?.incidentId && (
        <p className="mb-4 text-sm text-slate-600">
          Comparing <span className="font-medium text-navy">{seed.table || "this table"}</span> to its source.
          The result stays on the incident. Resolve stays a separate step.
        </p>
      )}

      {step === 1 && (
        <div className="space-y-4">
          <div>
            <div className="text-sm font-semibold text-navy">What do you want to test?</div>
            <p className="text-xs text-slate-500">
              Pick the type of validation. Compare two datasets — row <span className="font-medium">counts</span>,
              their <span className="font-medium">schema</span>, or <span className="font-medium">row-by-row data</span> —
              or run <span className="font-medium">quality rules</span> on a single dataset. Not sure? Start from a template below.
            </p>
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {KINDS.map((k) => (
              <button
                key={k.key}
                onClick={() => set({ kind: k.key })}
                className={`rounded-xl border p-4 text-left transition ${
                  s.kind === k.key ? "border-teal ring-2 ring-teal/20" : "border-slate-200 hover:border-slate-300"
                }`}
              >
                <div className="grid h-8 w-8 place-items-center rounded-lg bg-navy text-sm font-bold text-white">{k.glyph}</div>
                <div className="mt-2 text-sm font-semibold text-navy">{k.label}</div>
                <div className="text-xs text-slate-400">{k.blurb}</div>
              </button>
            ))}
          </div>
          <div className="pt-1">
            <div className="mb-1 text-xs font-medium text-slate-400">Or start from a template</div>
            <div className="flex flex-wrap gap-2">
              {TEMPLATES.map((t) => (
                <button
                  key={t.label}
                  onClick={() => set({ kind: t.kind, ...t.apply(s) })}
                  className="rounded-full border border-slate-200 px-3 py-1 text-xs text-slate-600 hover:border-teal hover:text-teal-700"
                >
                  {t.emoji} {t.label}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Compare — guided: step 2 source, step 3 target, step 4 mapping. */}
      {isCompare && step === 2 && (
        <DatasetListPanel
          title={`Source datasets — ${kindMeta.label}`}
          subtitle="Pick the source tables/files or add SQL datasets (any connection). Each shows an approximate row count; expand Preview to see sample rows."
          connections={connections}
          datasets={s.srcDatasets}
          onChange={(d) => set({ srcDatasets: d })}
        />
      )}
      {isCompare && step === 3 && (
        <DatasetListPanel
          title="Target datasets"
          subtitle="Pick the datasets to compare against. They can be a different connection, source type, or SQL — you'll map them next."
          connections={connections}
          datasets={s.dstDatasets}
          onChange={(d) => set({ dstDatasets: d })}
        />
      )}
      {isCompare && step === 4 && (
        <div className="space-y-3">
          <div>
            <div className="text-sm font-semibold text-navy">Map source → target</div>
            <p className="text-xs text-slate-500">Auto-map pairs by name (fuzzy), then expand any pair to review/adjust the fuzzy column mapping and key.</p>
          </div>
          <MappingStep
            src={s.srcDatasets} dst={s.dstDatasets} mappings={s.mappings}
            onMappings={(m) => set({ mappings: m })}
            showColumns={s.kind !== "counts_compare"}
            countsMode={s.kind === "counts_compare"}
            batchCond={{ direction: s.countDir, tolPct: s.tol, tolRows: s.tolRows }}
          />
          {s.kind === "counts_compare" && (
            <p className="text-xs text-slate-500">Expand <span className="font-medium">condition</span> on any pair to set a per-table tolerance — it overrides the batch default you'll set next.</p>
          )}
        </div>
      )}

      {!isCompare && step === 2 && (
        <div className="space-y-4">
          <div className="text-sm font-semibold text-navy">Choose the data — {kindMeta.label}</div>
            <div className="space-y-3">
              <DataPanel title="Dataset" tableOnly connections={connections} conn={s.dqConn} setConn={(v) => set({ dqConn: v })}
                schema={s.dqSchema} setSchema={(v) => set({ dqSchema: v })} mode="table" setMode={() => {}}
                table={s.dqTable} setTable={(v) => set({ dqTable: v })} sql="" setSql={() => {}} />

              {s.dqTable && specs && (
                <div>
                  <div className="mb-1 text-sm font-semibold text-navy">How is this table loaded?</div>
                  <TableLoadConfigRow
                    table={s.dqTable}
                    columns={dqCols}
                    loadingColumns={dqColsLoading}
                    specs={specs}
                    value={s.dqLoad}
                    onChange={(next) => set({ dqLoad: next })}
                    onAutoDetect={dqAutoDetect}
                    detecting={dqDetecting}
                  />
                </div>
              )}

              <div className="flex items-center justify-between">
                <div className="text-sm font-semibold text-navy">Rules ({s.dqRules.length}/{MAX_DQ_RULES})</div>
                <div className="flex items-center gap-2">
                  <button type="button" onClick={viewDqData} disabled={!s.dqConn || !s.dqTable}
                    title={!s.dqTable ? "Pick a dataset first" : "Preview ~20 sample rows to help write your rule SQL"}
                    className="rounded-lg border border-slate-300 px-3 py-1 text-xs font-medium text-slate-600 enabled:hover:border-teal enabled:hover:text-teal-700 disabled:opacity-40">
                    🔎 View data
                  </button>
                  <button type="button" onClick={addRule} disabled={s.dqRules.length >= MAX_DQ_RULES}
                    className="rounded-lg bg-teal px-3 py-1 text-xs font-medium text-white disabled:opacity-40">
                    + Add rule
                  </button>
                </div>
              </div>
              <p className="text-xs text-slate-500">
                Bundle up to {MAX_DQ_RULES} checks over this dataset — each runs and is scored independently.
                Tip: <span className="font-medium">View data</span> to see the columns and values before writing a rule.
              </p>
              {s.dqRules.map((r, i) => (
                <div key={i} className="space-y-2 rounded-lg border border-slate-200 p-3">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-semibold text-slate-400">#{i + 1}</span>
                    <TextInput value={r.name} placeholder="rule name"
                      onChange={(e) => setRule(i, { name: e.target.value })} />
                    {s.dqRules.length > 1 && (
                      <button type="button" onClick={() => removeRule(i)}
                        className="rounded px-2 py-1 text-xs text-rose-600 hover:bg-rose-50">Remove</button>
                    )}
                  </div>
                  <textarea value={r.sql} onChange={(e) => setRule(i, { sql: e.target.value })} rows={6}
                    placeholder="SELECT COUNT(*) AS v FROM {{table}} WHERE ..."
                    className="w-full resize-y rounded-lg border border-slate-300 px-3 py-2 font-mono text-xs outline-none focus:border-teal" style={{ minHeight: "8rem" }} />
                  <div className="grid grid-cols-3 gap-2">
                    <label className="text-xs text-slate-500">
                      Threshold
                      <TextInput type="number" value={r.threshold}
                        onChange={(e) => setRule(i, { threshold: e.target.value })} />
                    </label>
                    <label className="text-xs text-slate-500">
                      Dimension
                      <select value={r.dimension} onChange={(e) => setRule(i, { dimension: e.target.value })}
                        className="w-full rounded-lg border border-slate-300 bg-white px-2 py-2 text-sm">
                        {DIMENSIONS.map((d) => <option key={d} value={d}>{d}</option>)}
                      </select>
                    </label>
                    <label className="text-xs text-slate-500">
                      Severity
                      <select value={r.severity} onChange={(e) => setRule(i, { severity: e.target.value })}
                        className="w-full rounded-lg border border-slate-300 bg-white px-2 py-2 text-sm">
                        {SEVERITIES.map((sev) => <option key={sev} value={sev}>{sev}</option>)}
                      </select>
                    </label>
                  </div>
                </div>
              ))}
            </div>
          <Modal open={dqSampleOpen} onClose={() => setDqSampleOpen(false)} wide
            title={`Sample data — ${s.dqSchema ? `${s.dqSchema}.` : ""}${s.dqTable || "dataset"}`}>
            <DataSample loading={dqSampleLoading} err={dqSampleErr} data={dqSample} />
          </Modal>
        </div>
      )}

      {step === lastStep && (
        <div className="space-y-4">
          <div>
            <div className="text-sm font-semibold text-navy">{editMode ? "Configure" : isCompare ? "Configure & run" : "Review & run"}</div>
            <p className="text-xs text-slate-500">
              {editMode
                ? "Review the settings below and set an optional schedule, then Save changes. Saving updates the test case in place — it does not start a run."
                : isCompare
                ? "Name the test, tune how the compare runs (engine, tolerances, allowed differences), and set an optional schedule — then run it."
                : "Name the test and set an optional schedule, then run it."}
            </p>
          </div>
          <Field label="Test name *" hint="required — used in results, reports, and the executions list">
            <TextInput value={s.name} onChange={(e) => set({ name: e.target.value })} placeholder={review.slice(0, 50)} autoFocus />
          </Field>
          {isCompare && s.kind === "data_validation" && (
            <Field label="Default key columns" hint="The column(s) that uniquely identify a row, used to line up source & target. Comma-separated; blank = auto-detect per pair. A per-pair key set in the Mapping step overrides this.">
              <TextInput value={s.key} onChange={(e) => set({ key: e.target.value })} placeholder="ORDER_ID" />
            </Field>
          )}
          {isCompare && s.kind === "data_validation" && (
            <Field label="Compare engine" hint="Auto picks hash pushdown for Snowflake↔Snowflake — fastest, no data movement">
              <select
                className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm"
                value={s.engine}
                onChange={(e) => set({ engine: e.target.value })}
              >
                <option value="auto">Auto (recommended)</option>
                <option value="recursive">Recursive hash (same database — compares in-place)</option>
                <option value="hash">Hash pushdown (Snowflake → Snowflake)</option>
                <option value="spark">Spark (scale-out)</option>
                <option value="inmemory">In-memory</option>
              </select>
            </Field>
          )}
          {isCompare && s.kind === "data_validation" && (
            <Field label="Memory budget" hint="RAM for the in-memory/Spark path — guards against out-of-memory on large tables. Pushdown engines (Snowflake / same-database) use little memory regardless.">
              <select
                className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm"
                value={s.memoryGb}
                onChange={(e) => set({ memoryGb: e.target.value })}
              >
                <option value="">Auto{recommendedGb ? ` — detected ${recommendedGb} GB from table size` : " (recommended)"}</option>
                <option value="2">2 GB</option>
                <option value="4">4 GB</option>
                <option value="8">8 GB</option>
                <option value="16">16 GB</option>
                <option value="32">32 GB</option>
              </select>
            </Field>
          )}
          {isCompare && s.kind === "data_validation" && (
            <div className="space-y-2 rounded-lg border border-slate-200 bg-slate-50 p-4">
              <label className="flex cursor-pointer items-center gap-2 text-sm">
                <input type="checkbox" checked={s.failFast} onChange={(e) => set({ failFast: e.target.checked })} />
                <span className="font-medium text-navy">Fail fast</span>
                <span className="text-xs text-slate-400">— stop as soon as the tables clearly differ (recommended)</span>
              </label>
              {!s.failFast && (
                <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-700">
                  ⚠ With fail-fast off, the compare reads <strong>every</strong> differing row. On large, heavily-differing tables this can take <strong>much longer</strong> (minutes to hours) and run many more queries. It still stops at the time/query budget.
                </div>
              )}
              <label className="flex cursor-pointer items-center gap-2 text-sm">
                <input type="checkbox" checked={s.nullEqualsEmpty} onChange={(e) => set({ nullEqualsEmpty: e.target.checked })} />
                <span className="font-medium text-navy">Treat NULL and empty as equal</span>
                <span className="text-xs text-slate-400">— a NULL and an empty/blank string count as the same value</span>
              </label>
              <label className="flex cursor-pointer items-center gap-2 text-sm">
                <input type="checkbox" checked={s.showExtraColumns} onChange={(e) => set({ showExtraColumns: e.target.checked })} />
                <span className="font-medium text-navy">Capture non-compared columns</span>
                <span className="text-xs text-slate-400">— pull the other table columns too, shown in results (clearly marked “not compared”) for context</span>
              </label>
            </div>
          )}
          {isCompare && s.kind === "counts_compare" && (
            <div className="space-y-3 rounded-lg border border-slate-200 bg-slate-50 p-4">
              <div className="text-sm font-semibold text-navy">Pass condition</div>
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="text-slate-500">The source count may be</span>
                <select value={s.countDir} onChange={(e) => set({ countDir: e.target.value })}
                  className="rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-sm">
                  <option value="both">higher or lower than target</option>
                  <option value="source_higher">higher than target (never lower)</option>
                  <option value="source_lower">lower than target (never higher)</option>
                </select>
              </div>
              <div className="flex flex-wrap items-end gap-3">
                <span className="pb-2 text-sm text-slate-500">by at most</span>
                <label className="text-xs text-slate-500">%
                  <TextInput type="number" value={s.tol} onChange={(e) => set({ tol: e.target.value })} placeholder="e.g. 1" />
                </label>
                <span className="pb-2 text-xs text-slate-400">and / or</span>
                <label className="text-xs text-slate-500">rows
                  <TextInput type="number" value={s.tolRows} onChange={(e) => set({ tolRows: e.target.value })} placeholder="e.g. 1000" />
                </label>
              </div>
              <p className="text-[11px] text-slate-400">
                Passes if the gap is within <span className="font-medium">either</span> budget. {s.countDir === "both"
                  ? "Leave both at 0 to require an exact match."
                  : "Leave both blank/0 to allow any amount in that direction."}
                {" "}% is measured against the target count.
              </p>
            </div>
          )}
          {isCompare && s.kind === "data_validation" && (
            <div className="space-y-2 rounded-lg border border-slate-200 bg-slate-50 p-4">
              <div className="text-sm font-semibold text-navy">Allowed differences <span className="font-normal text-slate-400">— optional</span></div>
              <p className="text-xs text-slate-500">
                Leave blank to require an exact match. Set a budget to tolerate some differing rows (value diffs, source-only, target-only) before the test fails.
              </p>
              <div className="flex flex-wrap items-end gap-4">
                <label className="text-xs text-slate-500">Max differing rows
                  <TextInput type="number" value={s.maxDiffRows} onChange={(e) => set({ maxDiffRows: e.target.value })} placeholder="e.g. 0" />
                </label>
                <span className="pb-2 text-xs text-slate-400">and / or</span>
                <label className="text-xs text-slate-500">Max differing %
                  <TextInput type="number" value={s.maxDiffPct} onChange={(e) => set({ maxDiffPct: e.target.value })} placeholder="e.g. 0.1" />
                </label>
              </div>
            </div>
          )}
          {isCompare && (
            <Field label="Schedule" hint="Run this test on a recurring cron. Leave as Manual to run on demand only.">
              <ScheduleControl cron={s.schedule || null} nextRun={null} onSet={(c) => set({ schedule: c ?? "" })} />
            </Field>
          )}
          {detectedVars.length > 0 && (
            <div className="space-y-2 rounded-lg border border-teal/30 bg-teal/5 p-4">
              <div className="text-sm font-semibold text-navy">Runtime variables</div>
              <p className="text-xs text-slate-500">
                Supply a value for each {"{{variable}}"} in this test. These are recorded with the run.
              </p>
              <div className="grid grid-cols-2 gap-3">
                {detectedVars.map((v) => (
                  <Field key={v} label={`{{${v}}}`}>
                    <TextInput value={vars[v] ?? ""} onChange={(e) => setVars((p) => ({ ...p, [v]: e.target.value }))} />
                  </Field>
                ))}
              </div>
            </div>
          )}
          {/* Organize (source system / layer / tags) only applies to data-quality monitors. */}
          {!isCompare && (
            <div className="space-y-3 rounded-lg border border-slate-200 bg-slate-50 p-4">
              <div className="flex items-center gap-2 text-sm font-semibold text-navy">
                Organize
                <span className="rounded-full bg-navy/5 px-2 py-0.5 text-xs font-medium text-navy">
                  Project: {projectName || "General (default)"}
                </span>
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <Field label="Source system" hint="e.g. oracle, snowflake">
                  <TextInput value={org.source} onChange={(e) => setOrg({ ...org, source: e.target.value })} />
                </Field>
                <Field label="Layer">
                  <select value={org.layer} onChange={(e) => setOrg({ ...org, layer: e.target.value })}
                    className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm">
                    <option value="">None</option>
                    {["bronze", "silver", "gold"].map((l) => <option key={l} value={l}>{l}</option>)}
                  </select>
                </Field>
              </div>
              {allTags.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {allTags.map((t) => {
                    const on = org.tags.includes(t.name);
                    return (
                      <button key={t.name} type="button"
                        onClick={() => setOrg({ ...org, tags: on ? org.tags.filter((x) => x !== t.name) : [...org.tags, t.name] })}
                        className={`rounded-full px-2 py-0.5 text-xs ${on ? "bg-navy text-white" : "bg-slate-200 text-slate-600"}`}>
                        {t.name}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          )}
          <div className="rounded-lg bg-slate-50 p-4 text-sm text-slate-600">
            <span className="mr-1 font-semibold text-navy">This test will:</span>
            {review}
          </div>
        </div>
      )}

      {err && <p className="mt-3 text-xs text-rose-600">{err}</p>}

      <div className="mt-6 flex items-center justify-between">
        <Button variant="ghost" onClick={step === 1 ? cancel : () => setStep(step - 1)}>
          {step === 1 ? "Cancel" : "← Back"}
        </Button>
        {step < lastStep ? (
          <Button onClick={() => setStep(step + 1)} disabled={!canAdvance(step)}>
            Next →
          </Button>
        ) : (
          <div className="flex items-center gap-2">
            {canDry && (
              <span title="Run the real compare on ~1,000 sample rows without saving — check the key/mapping before the full run">
                <Button variant="ghost" onClick={dryRunPair} disabled={busy || dryLoading}>
                  {dryLoading ? "Testing…" : "🧪 Dry-run on sample"}
                </Button>
              </span>
            )}
            <Button onClick={editBatch ? saveBatch : editJob ? save : run} disabled={busy || !varsComplete || !s.name.trim()}>
              {busy ? (editMode ? "Saving…" : "Running…") : editMode ? "Save changes" : "Run test"}
            </Button>
          </div>
        )}
      </div>

      <Modal open={dryOpen} onClose={() => setDryOpen(false)} wide title="Dry-run on sample">
        <DryRunSummary loading={dryLoading} err={dryErr} data={dry} />
      </Modal>
    </div>
  );
}
