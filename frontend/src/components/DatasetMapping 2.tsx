import { useEffect, useState } from "react";
import { api } from "../api";
import type { Connection, MappingSuggestion } from "../types";
import { Combobox, TablePicker } from "./Combobox";
import { Modal } from "./ui";

/** Sparkles "magic" icon — explicitly violet so it stays visible on light/white button states. */
function SparkleIcon({ className = "h-3.5 w-3.5" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" className={`shrink-0 text-violet-500 ${className}`}>
      <path fillRule="evenodd" clipRule="evenodd" d="M9 4.5a.75.75 0 01.721.544l.813 2.846a3.75 3.75 0 002.576 2.576l2.846.813a.75.75 0 010 1.442l-2.846.813a3.75 3.75 0 00-2.576 2.576l-.813 2.846a.75.75 0 01-1.442 0l-.813-2.846a3.75 3.75 0 00-2.576-2.576l-2.846-.813a.75.75 0 010-1.442l2.846-.813A3.75 3.75 0 007.466 7.89l.813-2.846A.75.75 0 019 4.5zM18 1.5a.75.75 0 01.728.568l.258 1.036c.236.94.97 1.674 1.91 1.91l1.036.258a.75.75 0 010 1.456l-1.036.258c-.94.236-1.674.97-1.91 1.91l-.258 1.036a.75.75 0 01-1.456 0l-.258-1.036a2.625 2.625 0 00-1.91-1.91l-1.036-.258a.75.75 0 010-1.456l1.036-.258a2.625 2.625 0 001.91-1.91l.258-1.036A.75.75 0 0118 1.5zM16.5 15a.75.75 0 01.712.513l.394 1.183c.15.447.5.799.948.948l1.183.395a.75.75 0 010 1.422l-1.183.395c-.447.15-.799.5-.948.948l-.395 1.183a.75.75 0 01-1.422 0l-.395-1.183a1.5 1.5 0 00-.948-.948l-1.183-.395a.75.75 0 010-1.422l1.183-.395c.447-.15.799-.5.948-.948l.395-1.183A.75.75 0 0116.5 15z" />
    </svg>
  );
}

// A "dataset" is one comparable thing on either side: a table/view/file from any
// connection (Snowflake, SQL Server, Mongo, S3/JSON, …) OR an ad-hoc SQL query.
// Source and target are each just a list of datasets; the user maps them N:M.
export type Dataset = {
  id: string;
  connId: string;
  kind: "table" | "sql";
  schema?: string;
  db?: string;
  name: string; // table/file name, or a short label for a SQL query
  sql?: string;
  filter?: string; // optional WHERE predicate — only matching rows are compared
};
export type ColPair = { src: string; dst: string; include: boolean };
export type Mapping = {
  srcId: string; dstId: string; key?: string; keyMethod?: string; columns?: ColPair[]; colsLoaded?: boolean;
  // When editing an existing batch, the compare job this pair maps to (absent for a newly added pair).
  jobId?: string;
  // Per-table counts conditions (override the batch default for this pair): direction + tolerance.
  direction?: string; tolPct?: string; tolRows?: string;
};

let _seq = 0;
const newId = () => `ds_${Date.now().toString(36)}_${(_seq++).toString(36)}`;

/** Backend compare-side spec for one dataset (table/file → relation; sql → query). The
 *  optional `filter` (a SQL predicate) scopes the comparison to matching rows only. */
export function datasetSpec(d: Dataset): Record<string, unknown> {
  const base = d.kind === "sql"
    ? { connection_id: d.connId, sql: d.sql || "" }
    : { connection_id: d.connId, schema: d.schema || undefined, database: d.db || undefined, table: d.name };
  return d.filter?.trim() ? { ...base, filter: d.filter.trim() } : base;
}

// ─── fuzzy name matching (tables AND columns) ────────────────────────────────
const PREFIXES = [
  "brz_", "bronze_", "slv_", "silver_", "gld_", "gold_", "dim_", "fct_", "fact_",
  "stg_", "staging_", "raw_", "tmp_", "temp_", "vw_", "v_", "dwh_", "dw_",
  "src_", "tgt_", "source_", "target_", "c_", "t_", "d_", "f_",
];
/** Canonicalise a table/column name for matching: last path segment, lowercase, strip a
 *  known prefix, drop non-alphanumerics, crude de-pluralise. */
function normName(raw: string): string {
  let s = (raw.split(/[./\\]/).filter(Boolean).pop() ?? raw).toLowerCase();
  for (const p of PREFIXES) if (s.startsWith(p)) { s = s.slice(p.length); break; }
  s = s.replace(/[^a-z0-9]+/g, "");
  if (s.length > 3 && s.endsWith("s")) s = s.slice(0, -1);
  return s;
}
function levenshtein(a: string, b: string): number {
  const m = a.length, n = b.length;
  if (!m) return n;
  if (!n) return m;
  let prev = Array.from({ length: n + 1 }, (_, i) => i);
  let cur = new Array<number>(n + 1);
  for (let i = 1; i <= m; i++) {
    cur[0] = i;
    for (let j = 1; j <= n; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
    }
    [prev, cur] = [cur, prev];
  }
  return prev[n];
}
/** 0‥1 similarity between two raw names (1 = exact after normalisation). */
export function similarity(aRaw: string, bRaw: string): number {
  const a = normName(aRaw), b = normName(bRaw);
  if (!a || !b) return 0;
  if (a === b) return 1;
  const ratio = 1 - levenshtein(a, b) / Math.max(a.length, b.length);
  const contains = a.includes(b) || b.includes(a) ? 0.15 : 0;
  return Math.min(1, ratio + contains);
}
/** Greedy one-to-one match by name similarity, best matches first. Returns index pairs. */
function greedyMatch(srcNames: string[], dstNames: string[], threshold: number): [number, number][] {
  const cand: { s: number; d: number; score: number }[] = [];
  srcNames.forEach((a, s) => dstNames.forEach((b, d) => {
    const score = similarity(a, b);
    if (score >= threshold) cand.push({ s, d, score });
  }));
  cand.sort((x, y) => y.score - x.score);
  const usedS = new Set<number>(), usedD = new Set<number>(), out: [number, number][] = [];
  for (const c of cand) {
    if (usedS.has(c.s) || usedD.has(c.d)) continue;
    usedS.add(c.s); usedD.add(c.d);
    out.push([c.s, c.d]);
  }
  return out;
}
/** Greedy one-to-one dataset auto-map by name. */
export function autoMap(src: Dataset[], dst: Dataset[], threshold = 0.55): Mapping[] {
  return greedyMatch(src.map((d) => d.name), dst.map((d) => d.name), threshold)
    .map(([s, d]) => ({ srcId: src[s].id, dstId: dst[d].id }));
}
/** Fuzzy column auto-map between two column lists. */
export function autoMapColumns(srcCols: string[], dstCols: string[], threshold = 0.5): ColPair[] {
  return greedyMatch(srcCols, dstCols, threshold).map(([s, d]) => ({ src: srcCols[s], dst: dstCols[d], include: true }));
}

const fmtCount = (n: number) => n.toLocaleString();

/** Sample-rows viewer. Narrow tables render as a normal grid; WIDE tables (many columns)
 *  transpose to "column → sample values" with a column filter, so a 100-column table is scannable
 *  vertically instead of scrolling sideways forever. */
function SamplePreview({ columns, rows, max = 5 }: { columns: string[]; rows: unknown[][]; max?: number }) {
  const [filter, setFilter] = useState("");
  if (columns.length === 0) return <p className="text-[11px] text-slate-400">No sample rows.</p>;
  const cell = (v: unknown) => (v === null || v === undefined ? <span className="text-slate-300">∅</span> : String(v));
  const sampleRows = rows.slice(0, max);

  // Narrow: a plain horizontal grid is easiest to read.
  if (columns.length <= 12) {
    return (
      <div className="max-h-72 overflow-auto">
        <table className="w-full text-[11px]">
          <thead className="sticky top-0 bg-slate-50 text-left text-slate-500">
            <tr>{columns.map((c) => <th key={c} className="whitespace-nowrap px-2 py-1 font-semibold">{c}</th>)}</tr>
          </thead>
          <tbody>
            {sampleRows.map((row, i) => (
              <tr key={i} className="border-t border-slate-100">
                {row.map((v, j) => <td key={j} className="whitespace-nowrap px-2 py-1 font-mono text-slate-600">{cell(v)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  // Wide: transpose so columns run DOWN (filterable) and sample values run across.
  const needle = filter.trim().toLowerCase();
  const cols = columns.map((c, i) => ({ c, i })).filter(({ c }) => !needle || c.toLowerCase().includes(needle));
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-[11px] text-slate-400">{columns.length} columns · shown as column → sample values</span>
        <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="filter columns…"
          className="w-40 rounded border border-slate-300 px-2 py-0.5 text-[11px] outline-none focus:border-teal" />
      </div>
      <div className="max-h-72 overflow-auto rounded border border-slate-200">
        <table className="min-w-full text-[11px]">
          <thead className="sticky top-0 bg-slate-50 text-left text-slate-500">
            <tr>
              <th className="px-2 py-1 font-semibold">Column</th>
              {sampleRows.map((_, i) => <th key={i} className="px-2 py-1 font-medium">row {i + 1}</th>)}
            </tr>
          </thead>
          <tbody>
            {cols.map(({ c, i }) => (
              <tr key={c} className="border-t border-slate-100">
                <td className="whitespace-nowrap px-2 py-1 font-medium text-navy">{c}</td>
                {sampleRows.map((row, ri) => (
                  <td key={ri} className="max-w-[18rem] truncate px-2 py-1 font-mono text-slate-600" title={row[i] == null ? "" : String(row[i])}>{cell(row[i])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {cols.length === 0 && <p className="text-[11px] text-slate-400">No columns match “{filter}”.</p>}
    </div>
  );
}

// ─── one selected dataset: approx count + lazy sample preview ─────────────────
function DatasetRow({ d, connName, onRemove, onUpdate }: { d: Dataset; connName: string; onRemove: () => void; onUpdate: (patch: Partial<Dataset>) => void }) {
  const [count, setCount] = useState<{ count: number | null; approx: boolean } | null>(null);
  const [open, setOpen] = useState(false);
  const [showFilter, setShowFilter] = useState(!!d.filter);
  const [editing, setEditing] = useState(false);  // SQL datasets: inline query editor
  const [sample, setSample] = useState<{ columns: string[]; rows: unknown[][] } | null>(null);
  const [loadingSample, setLoadingSample] = useState(false);
  const sqlPreview = useSqlPreview(d.connId, d.db);  // used only for SQL datasets

  useEffect(() => {
    if (d.kind !== "table" || !d.schema) return;
    let alive = true;
    api.rowCount(d.connId, d.schema, d.name, d.db).then((r) => alive && setCount(r.data)).catch(() => undefined);
    return () => { alive = false; };
  }, [d.connId, d.schema, d.name, d.db, d.kind]);

  const loadSample = () => {
    if (sample || d.kind !== "table" || !d.schema) { setOpen((o) => !o); return; }
    setLoadingSample(true);
    setOpen(true);
    api.sampleRows(d.connId, d.schema, d.name, 10, d.db)
      .then((r) => setSample(r.data)).catch(() => setSample({ columns: [], rows: [] }))
      .finally(() => setLoadingSample(false));
  };

  return (
    <div className="border-b border-slate-100 last:border-0">
      <div className="flex items-center gap-2 px-3 py-2 text-sm">
        <span className="text-[11px] text-slate-400">{d.kind === "sql" ? "∑" : "▦"}</span>
        <span className="font-medium text-navy">{d.name}</span>
        <span className="text-[11px] text-slate-400">{connName}{d.schema ? ` · ${d.schema}` : ""}</span>
        <span className="ml-auto flex items-center gap-2">
          {d.kind === "table" && (
            count === null ? <span className="text-[11px] text-slate-300">counting…</span>
              : count.count === null ? <span className="text-[11px] text-slate-300">—</span>
              : <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-600">
                  {count.approx ? "~" : ""}{fmtCount(count.count)} rows
                </span>
          )}
          {d.kind === "table" && (
            <button onClick={loadSample} className="text-[11px] font-medium text-teal-700 hover:underline">
              {open ? "Hide" : "Preview"}
            </button>
          )}
          {d.kind === "sql" && (
            <button onClick={() => setEditing((e) => !e)} title="Edit query"
              className={`text-[11px] font-medium hover:underline ${editing ? "text-teal-700" : "text-slate-400 hover:text-slate-600"}`}>
              {editing ? "Close" : "✎ Edit"}
            </button>
          )}
          <button onClick={() => setShowFilter((f) => !f)}
            className={`text-[11px] font-medium hover:underline ${d.filter ? "text-teal-700" : "text-slate-400 hover:text-slate-600"}`}>
            {d.filter ? "✓ filter" : "filter"}
          </button>
          <button onClick={onRemove} className="text-slate-300 hover:text-rose-500">×</button>
        </span>
      </div>
      {showFilter && (
        <div className="flex items-center gap-2 border-t border-slate-100 bg-slate-50/60 px-3 py-1.5">
          <span className="text-[11px] font-medium text-slate-400">WHERE</span>
          <input
            value={d.filter ?? ""}
            onChange={(e) => onUpdate({ filter: e.target.value })}
            placeholder="e.g. REGION = 'EU' AND AMOUNT > 0"
            className="flex-1 rounded border border-slate-300 px-2 py-1 font-mono text-[11px] outline-none focus:border-teal"
          />
          <span className="text-[10px] text-slate-400">only matching rows are compared</span>
        </div>
      )}
      {editing && d.kind === "sql" && (
        <div className="space-y-1 border-t border-slate-100 bg-slate-50/60 px-3 py-2">
          <input value={d.name} onChange={(e) => onUpdate({ name: e.target.value })}
            className="w-full rounded border border-slate-300 px-2 py-1 text-xs outline-none focus:border-teal" placeholder="label" />
          <textarea value={d.sql} onChange={(e) => { onUpdate({ sql: e.target.value }); sqlPreview.reset(); }} rows={10}
            placeholder="SELECT …" className="w-full resize-y rounded border border-slate-300 px-2 py-1 font-mono text-[11px] outline-none focus:border-teal" style={{ minHeight: "12rem" }} />
          <div className="flex items-center gap-2">
            <button onClick={() => sqlPreview.validate(d.sql || "")} disabled={sqlPreview.checking || !(d.sql || "").trim()}
              className="rounded-lg border border-slate-300 px-2.5 py-1 text-[11px] font-medium text-slate-600 enabled:hover:border-teal enabled:hover:text-teal-700 disabled:opacity-40">
              {sqlPreview.checking ? "Validating…" : "Validate SQL"}
            </button>
            <SqlValidity result={sqlPreview.result} error={sqlPreview.error} />
          </div>
          <SqlPreviewOutput result={sqlPreview.result} error={sqlPreview.error} />
        </div>
      )}
      {open && d.kind === "table" && (
        <div className="border-t border-slate-100 bg-slate-50/60 px-3 py-2">
          {loadingSample ? <p className="text-[11px] text-slate-400">Loading sample…</p>
            : <SamplePreview columns={sample?.columns ?? []} rows={sample?.rows ?? []} max={10} />}
        </div>
      )}
    </div>
  );
}

// ─── per-side dataset list (one full step) ───────────────────────────────────
type PreviewResult = { ok: boolean; columns: string[]; rows: unknown[][]; row_count: number };

/** Runs a SQL query (capped) against a connection and tracks the validation state — the result
 *  sample, the DB error, and whether a check is in flight. Shared by the composer and the editor. */
function useSqlPreview(connId: string, db?: string) {
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<PreviewResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reset = () => { setResult(null); setError(null); };
  const validate = async (sql: string) => {
    setChecking(true); reset();
    try {
      const r = await api.previewSql(connId, sql || "", 10, db);
      if (r.warnings.length) setError(r.warnings.map((w) => w.message).join("; "));
      else setResult(r.data);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setChecking(false);
    }
  };
  return { checking, result, error, validate, reset };
}

/** The inline ✓ Valid / ✗ Invalid chip next to the Validate button. */
function SqlValidity({ result, error }: { result: PreviewResult | null; error: string | null }) {
  if (result?.ok)
    return (
      <span className="text-[11px] font-medium text-emerald-600">
        ✓ Valid · {result.columns.length} column{result.columns.length === 1 ? "" : "s"}{result.row_count === 0 ? " · 0 rows" : ""}
      </span>
    );
  if (error) return <span className="text-[11px] font-medium text-rose-600">✗ Invalid query</span>;
  return null;
}

/** The expandable output under the query: the DB error, or the columns + a tiny sample. */
function SqlPreviewOutput({ result, error }: { result: PreviewResult | null; error: string | null }) {
  return (
    <>
      {error && (
        <pre className="mt-1 max-h-28 overflow-auto whitespace-pre-wrap rounded bg-rose-50 px-2 py-1 font-mono text-[10px] text-rose-700">{error}</pre>
      )}
      {result?.ok && result.columns.length > 0 && (
        <div className="mt-1 rounded border border-slate-200 p-1">
          <SamplePreview columns={result.columns} rows={result.rows} max={5} />
          {result.row_count === 0 && <p className="px-2 py-1 text-[10px] text-slate-400">Query is valid but returned no rows.</p>}
        </div>
      )}
    </>
  );
}

/** Draft SQL composer. The query is NOT a dataset until the user validates it and clicks Add —
 *  typing here never pollutes the Selected list. Add is gated on a successful validation. */
function SqlComposer({ connId, connLabel, db, suggestedLabel, onAdd }: {
  connId: string;
  connLabel: string;
  db?: string;
  suggestedLabel: string;
  onAdd: (label: string, sql: string) => void;
}) {
  const [label, setLabel] = useState("");
  const [sql, setSql] = useState("");
  const { checking, result, error, validate, reset } = useSqlPreview(connId, db);

  const onSql = (v: string) => { setSql(v); reset(); };
  const add = () => {
    onAdd(label.trim() || suggestedLabel, sql.trim());
    setLabel(""); setSql(""); reset();
  };
  const canAdd = result?.ok === true;

  return (
    <div className="space-y-1 rounded-lg border border-dashed border-slate-300 p-2">
      <div className="flex items-center gap-2">
        <input value={label} onChange={(e) => setLabel(e.target.value)}
          className="flex-1 rounded border border-slate-300 px-2 py-1 text-xs outline-none focus:border-teal" placeholder={suggestedLabel} />
        <span className="text-[10px] text-slate-400">{connLabel}</span>
      </div>
      <textarea value={sql} onChange={(e) => onSql(e.target.value)} rows={10}
        placeholder="SELECT …" className="w-full resize-y rounded border border-slate-300 px-2 py-1 font-mono text-[11px] outline-none focus:border-teal" style={{ minHeight: "12rem" }} />
      <div className="flex items-center gap-2">
        <button onClick={() => validate(sql)} disabled={checking || !sql.trim()}
          className="rounded-lg border border-slate-300 px-2.5 py-1 text-[11px] font-medium text-slate-600 enabled:hover:border-teal enabled:hover:text-teal-700 disabled:opacity-40">
          {checking ? "Validating…" : "Validate SQL"}
        </button>
        <SqlValidity result={result} error={error} />
        <button onClick={add} disabled={!canAdd} title={canAdd ? "" : "Validate the query first"}
          className="ml-auto rounded-lg bg-teal px-2.5 py-1 text-[11px] font-semibold text-white enabled:hover:bg-teal-600 disabled:cursor-not-allowed disabled:opacity-40">
          + Add dataset
        </button>
      </div>
      <SqlPreviewOutput result={result} error={error} />
    </div>
  );
}

export function DatasetListPanel({
  title, subtitle, connections, datasets, onChange,
}: {
  title: string;
  subtitle?: string;
  connections: Connection[];
  datasets: Dataset[];
  onChange: (next: Dataset[]) => void;
}) {
  const [connId, setConnId] = useState(datasets[0]?.connId ?? connections[0]?.id ?? "");
  const [schema, setSchema] = useState(datasets.find((d) => d.connId === connId)?.schema ?? "");
  const [tab, setTab] = useState<"tables" | "sql">("tables");
  const [schemas, setSchemas] = useState<string[]>([]);
  const [tables, setTables] = useState<{ name: string; type: string }[]>([]);
  const [schemasLoading, setSchemasLoading] = useState(false);
  const [tablesLoading, setTablesLoading] = useState(false);

  useEffect(() => {
    if (!connId) return;
    setSchemasLoading(true);
    // Don't auto-pick a schema — wait for the user to choose. Defaulting to the first one
    // silently browsed the wrong schema and hid that a choice was needed. Keep an existing
    // pick for this connection (from already-selected datasets); otherwise start empty.
    setSchema(datasets.find((d) => d.connId === connId)?.schema ?? "");
    api.listSchemas(connId).then((r) => setSchemas(r.data.schemas ?? []))
      .catch(() => setSchemas([])).finally(() => setSchemasLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connId]);

  useEffect(() => {
    if (!connId || !schema) { setTables([]); return; }
    setTablesLoading(true);
    api.listCatalogTables(connId, schema).then((r) => setTables(r.data.tables))
      .catch(() => setTables([])).finally(() => setTablesLoading(false));
  }, [connId, schema]);

  const connName = (id: string) => connections.find((c) => c.id === id)?.name ?? id.slice(0, 6);
  const connOpts = connections.map((c) => ({ value: c.id, label: `${c.name} (${c.source_type})` }));

  const pickedHere = datasets.filter((d) => d.kind === "table" && d.connId === connId && d.schema === schema).map((d) => d.name);
  const setPickedHere = (names: string[]) => {
    const others = datasets.filter((d) => !(d.kind === "table" && d.connId === connId && d.schema === schema));
    const kept = datasets.filter((d) => d.kind === "table" && d.connId === connId && d.schema === schema && names.includes(d.name));
    const existing = new Set(kept.map((d) => d.name));
    const added = names.filter((n) => !existing.has(n)).map<Dataset>((n) => ({ id: newId(), connId, kind: "table", schema, name: n }));
    onChange([...others, ...kept, ...added]);
  };

  const update = (id: string, patch: Partial<Dataset>) => onChange(datasets.map((d) => (d.id === id ? { ...d, ...patch } : d)));
  const remove = (id: string) => onChange(datasets.filter((d) => d.id !== id));
  const sqlDatasets = datasets.filter((d) => d.kind === "sql");

  return (
    <div className="space-y-3">
      <div>
        <div className="text-sm font-semibold text-navy">{title}</div>
        {subtitle && <p className="text-xs text-slate-500">{subtitle}</p>}
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* Left: pick datasets */}
        <div className="space-y-2 rounded-xl border border-slate-200 bg-white p-4">
          <Combobox value={connId} onChange={setConnId} options={connOpts} allowCustom={false} placeholder="connection" />
          <div className="flex gap-1">
            {(["tables", "sql"] as const).map((t) => (
              <button key={t} onClick={() => setTab(t)}
                className={`rounded px-2 py-0.5 text-[11px] font-medium ${tab === t ? "bg-navy text-white" : "bg-slate-100 text-slate-600"}`}>
                {t === "tables" ? "Tables / Files" : "SQL query"}
              </button>
            ))}
          </div>
          {tab === "tables" ? (
            <>
              <Combobox value={schema} onChange={setSchema} options={schemas} loading={schemasLoading} placeholder="select a schema" />
              {!schema ? (
                <p className="py-2 text-center text-[11px] text-slate-400">Pick a schema to see its tables.</p>
              ) : tablesLoading ? (
                <p className="text-[11px] text-slate-400">Loading…</p>
              ) : (
                <TablePicker items={tables} value={pickedHere} onChange={setPickedHere} />
              )}
            </>
          ) : (
            <div className="space-y-2">
              <SqlComposer key={connId} connId={connId} connLabel={connName(connId)}
                suggestedLabel={`query ${sqlDatasets.length + 1}`}
                onAdd={(label, sql) => onChange([...datasets, { id: newId(), connId, kind: "sql", schema, name: label, sql }])} />
              {sqlDatasets.length > 0 && (
                <p className="text-[11px] text-slate-400">
                  {sqlDatasets.length} SQL dataset{sqlDatasets.length === 1 ? "" : "s"} added — edit any from the Selected list (✎) on the right.
                </p>
              )}
            </div>
          )}
        </div>

        {/* Right: selected datasets with counts + preview */}
        <div className="rounded-xl border border-slate-200 bg-white">
          <div className="border-b border-slate-100 px-3 py-2 text-xs font-bold uppercase tracking-wide text-slate-500">
            Selected · {datasets.length}
          </div>
          {datasets.length === 0 ? (
            <p className="px-3 py-6 text-center text-xs text-slate-400">Nothing selected yet — pick tables or add a SQL query.</p>
          ) : (
            <div className="max-h-[36rem] overflow-auto">
              {datasets.map((d) => <DatasetRow key={d.id} d={d} connName={connName(d.connId)} onRemove={() => remove(d.id)} onUpdate={(patch) => update(d.id, patch)} />)}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── per-pair column mapping (fuzzy, backend-suggested, editable) ─────────────
type SampleRow = { key: Record<string, unknown>; source: Record<string, unknown> | null; target: Record<string, unknown> | null };
type SideSample = { columns: string[]; rows: unknown[][] };
type SampleResp = {
  aligned: boolean;
  provided?: boolean;  // true → rows are the user's explicitly-requested keys, not a source sample
  warnings: string[];
  // aligned (a key was chosen): rows lined up by key
  key?: string[]; columns?: { src: string; dst: string }[]; sampled?: number; rows?: SampleRow[];
  // unaligned (no key): an independent sample from each side
  source?: SideSample; target?: SideSample;
};

function ColumnMap({ src, dst, mapping, onChange }: {
  src: Dataset; dst: Dataset;
  mapping: Mapping;
  onChange: (patch: Partial<Mapping>) => void;
}) {
  const [loading, setLoading] = useState(false);
  const [dstCols, setDstCols] = useState<string[]>([]);
  const [suggestion, setSuggestion] = useState<MappingSuggestion | null>(null);
  const [mode, setMode] = useState<"fuzzy" | "deep">("fuzzy");
  // "View sample data": pull ~20 keys from source, fetch those rows from both sides, show aligned.
  const [sample, setSample] = useState<SampleResp | null>(null);
  const [sampleOpen, setSampleOpen] = useState(false);
  const [sampleLoading, setSampleLoading] = useState(false);
  const [sampleErr, setSampleErr] = useState<string | null>(null);
  // Explicit key look-up: one text field per key column, one grid row per key the user wants to see.
  const [keyRows, setKeyRows] = useState<string[][]>([]);

  // Fuzzy = match on column names only (instant). Deep = also sample data and match the leftover
  // columns by value overlap (slower, probabilistic). `replace` overwrites current column rows
  // with the new mapping (used when the user switches mode); the first load keeps any prior edits.
  // Key detection is part of mapping (a compare can't line up rows without a key), so we always
  // ask for the key here instead of behind a separate button.
  const runMapping = async (m: "fuzzy" | "deep", replace: boolean) => {
    setLoading(true);
    try {
      const r = await api.suggestMapping(datasetSpec(src), datasetSpec(dst), true, m);
      const s = r.data;
      setSuggestion(s);
      setMode(m);
      setDstCols(s.target_columns || []);
      const cols: ColPair[] = (s.source_columns || []).map((srcName) => {
        const mm = (s.mappings || []).find((x) => x.source === srcName);
        return { src: srcName, dst: mm?.target ?? "", include: !!mm };
      });
      onChange({
        colsLoaded: true,
        key: mapping.key || (s.source_key || []).join(", "),
        keyMethod: s.key_method,
        columns: replace || !mapping.columns ? cols : mapping.columns,
      });
    } catch {
      onChange({ colsLoaded: true });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (mapping.colsLoaded) return;
    runMapping("fuzzy", false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mapping.colsLoaded]);

  const cols = mapping.columns ?? [];
  const confOf = (srcCol: string, dstCol: string) => {
    const m = suggestion?.mappings.find((x) => x.source === srcCol);
    return m && m.target === dstCol ? m : null;
  };
  // Target options: the loaded target columns, plus any already-saved target names (so an edited
  // test case shows its stored mapping even before/without a fresh column fetch).
  const dstColSet = new Set<string>([...dstCols, ...cols.map((c) => c.dst).filter(Boolean)]);
  const dstOpts = [{ value: "", label: "— none —" }, ...[...dstColSet].map((c) => ({ value: c, label: c }))];
  const setCol = (i: number, patch: Partial<ColPair>) =>
    onChange({ columns: cols.map((c, j) => (j === i ? { ...c, ...patch } : c)) });
  const setAll = (include: boolean) => onChange({ columns: cols.map((c) => ({ ...c, include })) });
  const includedCount = cols.filter((c) => c.include && c.dst).length;
  const keyMethod = mapping.keyMethod ?? suggestion?.key_method;
  // Key columns (PK-style) uniquely identify a row so source & target rows can be lined up.
  // They're the SOURCE column names; the per-row target dropdown handles differing names.
  const keyCols = new Set((mapping.key ?? "").split(",").map((k) => k.trim()).filter(Boolean));
  const toggleKey = (col: string, on: boolean) => {
    const next = new Set(keyCols);
    if (on) next.add(col); else next.delete(col);
    onChange({ key: cols.filter((c) => next.has(c.src)).map((c) => c.src).join(", "), keyMethod: "manual" });
  };

  const keyList = [...keyCols];

  // Fetch sample rows from both sides, aligned by key. With no `keyValues` we sample ~20 keys from
  // the source; with `keyValues` we look up exactly the keys the user typed in.
  const viewSample = async (keyValues?: string[][]) => {
    setSampleOpen(true); setSampleLoading(true); setSampleErr(null); setSample(null);
    if (keyList.length && keyRows.length === 0) setKeyRows([keyList.map(() => "")]);  // seed one input row
    try {
      const shown = cols.filter((c) => c.dst || keyCols.has(c.src));
      const column_mappings: Record<string, string> = {};
      for (const c of shown) if (c.dst) column_mappings[c.src] = c.dst;
      const r = await api.sampleByKey({
        source: datasetSpec(src),
        target: datasetSpec(dst),
        key: keyList,
        ...(keyValues && keyValues.length ? { key_values: keyValues } : {}),
        columns: shown.map((c) => c.src),
        column_mappings,
        limit: keyValues && keyValues.length ? Math.min(keyValues.length, 100) : 20,
      });
      setSample(r.data);
    } catch (e) {
      setSampleErr((e as Error).message);
    } finally {
      setSampleLoading(false);
    }
  };

  // Submit the typed-in keys: keep only rows where every key column is filled, pass them through.
  const lookUpKeys = () => {
    const filled = keyRows
      .map((r) => r.map((v) => v.trim()))
      .filter((r) => r.length === keyList.length && r.every((v) => v !== ""));
    if (filled.length) viewSample(filled);
  };
  const setKeyCell = (ri: number, ci: number, v: string) =>
    setKeyRows((rows) => rows.map((r, i) => (i === ri ? r.map((c, j) => (j === ci ? v : c)) : r)));
  const addKeyRow = () => setKeyRows((rows) => [...rows, keyList.map(() => "")]);
  const removeKeyRow = (ri: number) => setKeyRows((rows) => rows.filter((_, i) => i !== ri));

  if (loading)
    return (
      <div className="flex flex-col items-center gap-2 bg-slate-50/60 px-3 py-8 text-center">
        {mode === "deep" ? (
          <SparkleIcon className="h-6 w-6 animate-spin" />
        ) : (
          <span className="h-6 w-6 animate-spin rounded-full border-2 border-slate-300 border-t-teal" />
        )}
        <div className="text-xs font-semibold text-navy">
          {mode === "deep" ? "Analyzing data to match columns & detect keys…" : "Matching columns by name…"}
        </div>
        {mode === "deep" && (
          <div className="text-[11px] text-slate-400">Sampling rows from both sides — this can take a while on large tables.</div>
        )}
      </div>
    );
  const ROW = "grid grid-cols-[34px,1fr,14px,1fr,64px,70px] items-center gap-2";
  const modeBtn = (active: boolean) =>
    `rounded-md px-2 py-0.5 text-[11px] font-medium transition ${active ? "bg-white text-navy shadow-sm" : "text-slate-500 hover:text-slate-700"}`;
  return (
    <div className="space-y-2 bg-slate-50/60 px-3 py-2">
      {/* Peek at real rows for the detected keys, pulled from BOTH sides and lined up. */}
      <div className="flex items-center justify-end">
        <button
          onClick={() => viewSample()}
          title={keyCols.size === 0 ? "No key yet — shows 20 rows from source and target separately" : "Fetch ~20 sample rows for these keys from source and target, lined up side by side"}
          className="text-[11px] font-medium text-teal-700 hover:underline">
          🔎 View sample data
        </button>
      </div>
      {/* Match mode: fuzzy (names) vs deep (samples data, probabilistic) */}
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <span className="font-semibold text-slate-500">Match columns by:</span>
        <div className="inline-flex rounded-lg border border-slate-200 bg-slate-100 p-0.5">
          <button onClick={() => runMapping("fuzzy", true)} className={modeBtn(mode === "fuzzy")} title="Match on column names only (instant)">Fuzzy Mapping</button>
          <button onClick={() => runMapping("deep", true)} className={`${modeBtn(mode === "deep")} inline-flex items-center gap-1`} title="Sample data and match renamed columns by value overlap">
            <SparkleIcon /> Deep Data Analysis Mapping
          </button>
        </div>
        {mode === "deep"
          ? <span className="text-slate-400">matched by sampling data — confirm the % probabilities below</span>
          : <span className="text-slate-400">matched by name — switch to ✨ Deep Data Analysis to line up renamed columns</span>}
      </div>

      {/* Tell the user plainly what we COULDN'T match — don't let unmapped columns pass silently. */}
      {(() => {
        const unmapped = cols.filter((c) => !c.dst).map((c) => c.src);
        if (unmapped.length === 0) return null;
        return (
          <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
            <span className="font-semibold text-amber-800">Couldn't auto-match {unmapped.length} column{unmapped.length === 1 ? "" : "s"}:</span>{" "}
            {unmapped.join(", ")}.{" "}
            {mode === "fuzzy"
              ? "Try ✨ Deep Data Analysis Mapping (matches by values), or pick a target below."
              : "No confident data match found — pick a target below, or they'll be left out of the compare."}
          </div>
        );
      })()}

      {/* Type mismatches are the #1 cause of false differences (e.g. 1 vs "1"). Call them out. */}
      {(() => {
        const mism = (suggestion?.mappings ?? []).filter(
          (m) => m.type_match === false && cols.some((c) => c.src === m.source && c.dst === m.target && c.include),
        );
        if (mism.length === 0) return null;
        return (
          <div className="rounded-lg border border-rose-300 bg-rose-50 px-3 py-2 text-[11px] text-rose-800">
            <span className="font-semibold">⚠ {mism.length} column{mism.length === 1 ? "" : "s"} with a type mismatch:</span>{" "}
            {mism.map((m) => `${m.source} (${m.src_type}→${m.dst_type})`).join(", ")}.{" "}
            Different types can read as differences even when the values are logically equal — cast/normalise them, or exclude the column.
          </div>
        );
      })()}

      {/* Key columns — identified with the mapping (a compare lines rows up by key). Tick 🔑 below
          to adjust; re-run Fuzzy/Deep above to re-detect. */}
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <span className="font-semibold text-slate-500">Key columns:</span>
        {keyCols.size === 0 ? (
          <span className="text-amber-600">none identified — tick 🔑 below to pick the key</span>
        ) : (
          [...keyCols].map((k) => <span key={k} className="rounded bg-teal/10 px-1.5 py-0.5 font-medium text-teal-700">🔑 {k}</span>)
        )}
        {keyMethod === "primary_key" && <span className="rounded bg-teal/10 px-1.5 py-0.5 text-[10px] font-medium text-teal-700">from PK</span>}
        {keyMethod === "detected" && <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">auto-detected</span>}
        {keyMethod === "manual" && keyCols.size > 0 && <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">manual</span>}
        {keyMethod !== "primary_key" && keyCols.size > 0 && suggestion?.key_uniqueness_pct != null && (
          <span className="text-slate-400">· {Math.round(suggestion.key_uniqueness_pct)}% unique</span>
        )}
      </div>

      {/* A non-unique key can't line rows up reliably → inflated/garbage differences. Warn loudly. */}
      {keyMethod !== "primary_key" && keyCols.size > 0 && suggestion?.key_uniqueness_pct != null && suggestion.key_uniqueness_pct < 100 && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-[11px] text-amber-800">
          ⚠ This key is only <span className="font-semibold">{Math.round(suggestion.key_uniqueness_pct)}% unique</span> — duplicate key values can&apos;t be matched one-to-one, which shows up as false differences and &quot;ambiguous&quot; rows. Add another column to the key (tick more 🔑) or choose a different one.
        </div>
      )}

      {cols.length === 0 ? <p className="text-[11px] text-slate-400">No columns found.</p> : (
        <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
          {/* Header row makes the source → target direction explicit (names can differ). */}
          <div className={`${ROW} border-b border-slate-100 bg-slate-50 px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400`}>
            <span className="text-center">Key</span>
            <span>Source column</span>
            <span />
            <span>Target column</span>
            <span className="text-center">Compare</span>
            <span />
          </div>
          {cols.map((c, i) => {
            const conf = confOf(c.src, c.dst);
            const isKey = keyCols.has(c.src);
            return (
              <div key={i} className={`${ROW} border-b border-slate-50 px-2 py-1 text-[11px] last:border-0 ${isKey ? "bg-teal/5" : ""}`}>
                <span className="text-center">
                  <input type="checkbox" checked={isKey} onChange={(e) => toggleKey(c.src, e.target.checked)} title="use as key column" />
                </span>
                <span className={`truncate font-medium ${isKey ? "text-teal-800" : "text-navy"}`} title={c.src}>{c.src}</span>
                <span className="text-slate-300">→</span>
                <select value={c.dst} onChange={(e) => setCol(i, { dst: e.target.value })}
                  className="w-full rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[11px] outline-none focus:border-teal">
                  {dstOpts.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
                <span className="text-center">
                  <input type="checkbox" checked={c.include} disabled={isKey}
                    onChange={(e) => setCol(i, { include: e.target.checked })}
                    title={isKey ? "key columns match rows; they aren't value-compared" : "compare this column's values"} />
                </span>
                {conf && conf.type_match === false ? (
                  <span className="truncate rounded bg-rose-50 px-1 py-0.5 text-center text-[10px] font-medium text-rose-700"
                    title={`Type mismatch: source ${conf.src_type} vs target ${conf.dst_type}. Values can show as differences even when logically equal — cast/normalise, or exclude this column.`}>
                    ⚠ {conf.src_type}→{conf.dst_type}
                  </span>
                ) : conf ? (
                  conf.method === "exact" || conf.confidence >= 0.99
                    ? <span className="rounded bg-emerald-50 px-1 py-0.5 text-center text-[10px] text-emerald-700">exact</span>
                    : <span className="rounded bg-amber-50 px-1 py-0.5 text-center text-[10px] text-amber-700" title={conf.method.replace(/_/g, " ").toLowerCase()}>~{Math.round(conf.confidence * 100)}%</span>
                ) : c.dst ? <span className="rounded bg-slate-100 px-1 py-0.5 text-center text-[10px] text-slate-500">manual</span> : <span className="text-center text-[10px] text-slate-300">—</span>}
              </div>
            );
          })}
        </div>
      )}
      <div className="flex items-center justify-between text-[10px]">
        <span className="text-slate-400">{includedCount} compared · {cols.length - includedCount} excluded · {keyCols.size} key</span>
        <span className="flex gap-2">
          <button onClick={() => setAll(true)} className="text-teal-700 hover:underline">compare all</button>
          <button onClick={() => setAll(false)} className="text-slate-400 hover:text-slate-600">exclude all</button>
        </span>
      </div>

      <Modal open={sampleOpen} onClose={() => setSampleOpen(false)} wide
        title={`Sample data — ${src.name} vs ${dst.name}`}>
        {/* Look up specific keys: one field per key column, one grid row per key to fetch. */}
        {keyList.length > 0 && (
          <div className="mb-3 rounded-lg border border-slate-200 bg-slate-50/70 px-3 py-2">
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-[11px] font-semibold text-navy">Look up specific keys</span>
              <span className="text-[11px] text-slate-400">key: {keyList.join(", ")}</span>
            </div>
            <div className="space-y-1.5">
              {keyRows.map((row, ri) => (
                <div key={ri} className="flex items-center gap-1.5">
                  {keyList.map((k, ci) => (
                    <input
                      key={k}
                      value={row[ci] ?? ""}
                      onChange={(e) => setKeyCell(ri, ci, e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") lookUpKeys(); }}
                      placeholder={k}
                      className="w-28 rounded-md border border-slate-200 px-2 py-1 text-[11px] focus:border-teal focus:outline-none"
                    />
                  ))}
                  <button onClick={() => removeKeyRow(ri)} title="Remove this key"
                    className="px-1 text-slate-400 hover:text-rose-500">✕</button>
                </div>
              ))}
            </div>
            <div className="mt-2 flex items-center gap-2">
              <button onClick={addKeyRow}
                className="rounded-md border border-slate-200 bg-white px-2 py-1 text-[11px] font-medium text-slate-600 hover:border-teal hover:text-teal-700">
                + add key
              </button>
              <button onClick={lookUpKeys}
                disabled={!keyRows.some((r) => r.some((v) => v.trim() !== ""))}
                className="rounded-md bg-teal px-3 py-1 text-[11px] font-semibold text-white disabled:opacity-40">
                Look up
              </button>
              <span className="text-[11px] text-slate-400">Fetches source &amp; target rows for exactly these keys.</span>
            </div>
          </div>
        )}
        <SampleDataView loading={sampleLoading} err={sampleErr} data={sample} />
      </Modal>
    </div>
  );
}

/** Side-by-side sample rows for the chosen keys: one key per block, a Source row and a Target row,
 *  with cells that differ highlighted. Values read live from both sides (table / SQL / file). */
function SampleDataView({ loading, err, data }: {
  loading: boolean; err: string | null; data: SampleResp | null;
}) {
  if (loading) return (
    <div className="flex flex-col items-center gap-2 px-1 py-10 text-center">
      <span className="h-6 w-6 animate-spin rounded-full border-2 border-slate-300 border-t-teal" />
      <div className="text-xs font-semibold text-navy">Fetching sample rows…</div>
      <div className="text-[11px] text-slate-400">Reading from source &amp; target — this can take a moment on large datasets.</div>
    </div>
  );
  if (err) return <p className="px-1 py-4 text-xs text-rose-600">{err}</p>;
  if (!data) return null;
  const fmt = (v: unknown) => (v === null || v === undefined ? "∅" : String(v));
  const same = (a: unknown, b: unknown) => fmt(a) === fmt(b);

  // No key chosen → show each side's own 20-row sample, separately (no alignment possible).
  if (!data.aligned) {
    return (
      <div className="space-y-4">
        <p className="text-xs text-slate-500">
          No key column chosen yet, so rows can&apos;t be lined up — showing an independent sample from each side.
          Pick a 🔑 key below to see them aligned and spot differences.
        </p>
        {data.warnings?.map((w, i) => <p key={i} className="rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-700">{w}</p>)}
        <SideTable title="Source" sample={data.source} fmt={fmt} />
        <SideTable title="Target" sample={data.target} fmt={fmt} />
      </div>
    );
  }

  const pairs = data.columns ?? [];
  const rows = data.rows ?? [];
  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        {data.sampled} key{data.sampled === 1 ? "" : "s"} sampled from the source; the same keys were fetched from the target.
        Cells that differ are highlighted. Key column(s): {(data.key ?? []).map((k) => <span key={k} className="rounded bg-teal/10 px-1 font-medium text-teal-700">🔑 {k}</span>)}
      </p>
      {data.warnings?.map((w, i) => <p key={i} className="rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-700">{w}</p>)}
      <div className="overflow-auto rounded-lg border border-slate-200">
        <table className="w-full text-[11px]">
          <thead className="bg-slate-50 text-left text-[10px] uppercase tracking-wide text-slate-400">
            <tr>
              <th className="px-2 py-1 font-semibold">Key</th>
              <th className="px-2 py-1 font-semibold">Side</th>
              {pairs.map((p) => (
                <th key={p.src} className="whitespace-nowrap px-2 py-1 font-semibold" title={p.src === p.dst ? p.src : `${p.src} → ${p.dst}`}>
                  {p.src}{p.src !== p.dst && <span className="text-slate-300"> → {p.dst}</span>}
                </th>
              ))}
            </tr>
          </thead>
          {rows.map((r, i) => {
              const keyLabel = Object.values(r.key).map(String).join(" · ");
              return (
                <tbody key={i} className="border-t border-slate-200">
                  <tr className="bg-white">
                    <td rowSpan={2} className="px-2 py-1 align-top font-mono font-medium text-navy">{keyLabel}</td>
                    <td className="px-2 py-1 text-slate-400">source</td>
                    {pairs.map((p) => <td key={p.src} className="whitespace-nowrap px-2 py-1 font-mono text-slate-700">{r.source ? fmt(r.source[p.src]) : "—"}</td>)}
                  </tr>
                  <tr className="bg-slate-50/50">
                    <td className="px-2 py-1 text-slate-400">target</td>
                    {!r.target
                      ? <td colSpan={pairs.length} className="px-2 py-1 text-rose-600">— no row with this key in target —</td>
                      : pairs.map((p) => {
                          const sv = r.source?.[p.src], tv = r.target?.[p.dst];
                          const diff = r.source && !same(sv, tv);
                          return <td key={p.dst} className={`whitespace-nowrap px-2 py-1 font-mono ${diff ? "bg-rose-50 font-semibold text-rose-700" : "text-slate-700"}`}>{fmt(tv)}</td>;
                        })}
                  </tr>
                </tbody>
              );
            })}
        </table>
      </div>
    </div>
  );
}

/** One side's plain columns × rows sample (used in the unaligned, no-key view). */
function SideTable({ title, sample, fmt }: {
  title: string; sample?: { columns: string[]; rows: unknown[][] }; fmt: (v: unknown) => string;
}) {
  const cols = sample?.columns ?? [];
  const rows = sample?.rows ?? [];
  return (
    <div className="space-y-1">
      <div className="text-xs font-semibold text-navy">{title} <span className="font-normal text-slate-400">· {rows.length} row{rows.length === 1 ? "" : "s"}</span></div>
      {cols.length === 0 ? (
        <p className="rounded-lg border border-slate-200 bg-slate-50 px-2 py-3 text-center text-[11px] text-slate-400">No rows returned.</p>
      ) : (
        <div className="overflow-auto rounded-lg border border-slate-200">
          <table className="w-full text-[11px]">
            <thead className="bg-slate-50 text-left text-[10px] uppercase tracking-wide text-slate-400">
              <tr>{cols.map((c) => <th key={c} className="whitespace-nowrap px-2 py-1 font-semibold">{c}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i} className="border-t border-slate-100">
                  {cols.map((_, j) => <td key={j} className="whitespace-nowrap px-2 py-1 font-mono text-slate-700">{fmt(row[j])}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── mapping step: table pairs (fuzzy) + per-pair column mapping ──────────────
/** Per-table pass condition for a counts-compare pair — direction + bounded tolerance. Blank
 *  fields fall back to the batch-level default (shown as the placeholder). */
function CountConditionRow({ m, batchCond, onChange }: {
  m: Mapping; batchCond?: { direction: string; tolPct: string; tolRows: string };
  onChange: (patch: Partial<Mapping>) => void;
}) {
  const dirDefault = batchCond?.direction ?? "both";
  const dirLabel: Record<string, string> = {
    both: "higher or lower", source_higher: "higher only", source_lower: "lower only",
  };
  return (
    <div className="space-y-2 border-t border-slate-100 bg-slate-50/60 px-3 py-2 text-[11px]">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold text-slate-500">This table — source count may be</span>
        <select value={m.direction ?? ""} onChange={(e) => onChange({ direction: e.target.value })}
          className="rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[11px] outline-none focus:border-teal">
          <option value="">default ({dirLabel[dirDefault]})</option>
          <option value="both">higher or lower</option>
          <option value="source_higher">higher only (never lower)</option>
          <option value="source_lower">lower only (never higher)</option>
        </select>
        <span className="text-slate-500">by at most</span>
        <input type="number" value={m.tolPct ?? ""} onChange={(e) => onChange({ tolPct: e.target.value })}
          placeholder={batchCond?.tolPct || "0"} className="w-16 rounded border border-slate-300 px-1.5 py-0.5 text-[11px] outline-none focus:border-teal" />
        <span className="text-slate-400">%</span>
        <span className="text-slate-400">and/or</span>
        <input type="number" value={m.tolRows ?? ""} onChange={(e) => onChange({ tolRows: e.target.value })}
          placeholder={batchCond?.tolRows || "0"} className="w-20 rounded border border-slate-300 px-1.5 py-0.5 text-[11px] outline-none focus:border-teal" />
        <span className="text-slate-400">rows</span>
      </div>
      <p className="text-[10px] text-slate-400">Blank = use the batch default from the last step. Passes if the gap is within either budget.</p>
    </div>
  );
}

export function MappingStep({ src, dst, mappings, onMappings, showColumns = true, countsMode = false, batchCond }: {
  src: Dataset[];
  dst: Dataset[];
  mappings: Mapping[];
  onMappings: (m: Mapping[]) => void;
  showColumns?: boolean;
  countsMode?: boolean;  // counts compare: per-pair pass condition (direction + tolerance)
  batchCond?: { direction: string; tolPct: string; tolRows: string };  // batch default (shown as the fallback)
}) {
  // Auto-open the first pair that already has a column mapping (i.e. when editing an existing test
  // case), so the mapping is visible without an extra click. Collapsed by default when creating.
  const [expanded, setExpanded] = useState<number | null>(() => {
    const i = mappings.findIndex((m) => (m.columns?.length ?? 0) > 0);
    return i >= 0 ? i : null;
  });
  const byId = (list: Dataset[], id: string) => list.find((d) => d.id === id);
  const label = (d?: Dataset) => (d ? `${d.name}${d.kind === "sql" ? " (SQL)" : ""}` : "—");
  const srcOpts = src.map((d) => ({ value: d.id, label: label(d) }));
  const dstOpts = dst.map((d) => ({ value: d.id, label: label(d) }));

  const setRow = (i: number, patch: Partial<Mapping>) =>
    onMappings(mappings.map((m, j) => (j === i ? { ...m, ...patch } : m)));
  const addRow = () => onMappings([...mappings, { srcId: src[0]?.id ?? "", dstId: dst[0]?.id ?? "" }]);
  const delRow = (i: number) => onMappings(mappings.filter((_, j) => j !== i));
  const runAutoMap = () => { const a = autoMap(src, dst); onMappings(a.length ? a : mappings); };

  const mappedSrc = new Set(mappings.map((m) => m.srcId));
  const unmappedSrc = src.filter((d) => !mappedSrc.has(d.id));

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4">
      <div className="mb-2 flex items-center justify-between">
        <div className="text-sm font-semibold text-navy">Mapping <span className="text-slate-400">· {mappings.length} pair{mappings.length === 1 ? "" : "s"}</span></div>
        <div className="flex items-center gap-2">
          <button onClick={runAutoMap} disabled={!src.length || !dst.length}
            className="rounded-lg bg-teal/10 px-3 py-1.5 text-xs font-semibold text-teal-700 hover:bg-teal/20 disabled:opacity-40">
            ✨ Auto-map by name
          </button>
          <button onClick={addRow} disabled={!src.length || !dst.length}
            className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 hover:border-teal disabled:opacity-40">
            + Add row
          </button>
        </div>
      </div>

      {mappings.length === 0 ? (
        <p className="rounded-lg bg-slate-50 px-3 py-3 text-center text-xs text-slate-500">
          <span className="font-medium">Auto-map by name</span> (exact + fuzzy) or add rows to pair datasets yourself.
        </p>
      ) : (
        <div className="space-y-1.5">
          {mappings.map((m, i) => {
            const a = byId(src, m.srcId), b = byId(dst, m.dstId);
            const score = a && b ? similarity(a.name, b.name) : 0;
            const badge = !a || !b ? null : score === 1
              ? <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">exact</span>
              : score >= 0.55
              ? <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">~{Math.round(score * 100)}%</span>
              : <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-500">manual</span>;
            // Column mapping works for any pair — the backend introspects columns from a
            // table/file or a SQL query alike.
            const canCols = showColumns && !!a && !!b;
            const isOpen = expanded === i;
            const hasCond = countsMode && (!!m.direction || (m.tolPct ?? "") !== "" || (m.tolRows ?? "") !== "");
            return (
              <div key={i} className="rounded-lg border border-slate-200">
                <div className="flex items-center gap-2 p-1.5">
                  <div className="flex-1"><Combobox value={m.srcId} onChange={(v) => setRow(i, { srcId: v, colsLoaded: false, columns: undefined })} options={srcOpts} allowCustom={false} placeholder="source" /></div>
                  <span className="text-slate-300">⇄</span>
                  <div className="flex-1"><Combobox value={m.dstId} onChange={(v) => setRow(i, { dstId: v, colsLoaded: false, columns: undefined })} options={dstOpts} allowCustom={false} placeholder="target" /></div>
                  <div className="w-12 text-center">{badge}</div>
                  {canCols && (
                    <button onClick={() => setExpanded(isOpen ? null : i)} className="text-[11px] font-medium text-teal-700 hover:underline">
                      {isOpen ? "Hide cols" : m.columns ? `${m.columns.length} cols` : "Map cols"}
                    </button>
                  )}
                  {countsMode && !!a && !!b && (
                    <button onClick={() => setExpanded(isOpen ? null : i)}
                      className={`text-[11px] font-medium hover:underline ${hasCond ? "text-teal-700" : "text-slate-400 hover:text-slate-600"}`}>
                      {isOpen ? "Hide" : hasCond ? "✓ condition" : "condition"}
                    </button>
                  )}
                  <button onClick={() => delRow(i)} className="text-slate-400 hover:text-rose-500">×</button>
                </div>
                {canCols && isOpen && (
                  <ColumnMap src={a!} dst={b!} mapping={m} onChange={(patch) => setRow(i, patch)} />
                )}
                {countsMode && isOpen && !!a && !!b && (
                  <CountConditionRow m={m} batchCond={batchCond} onChange={(patch) => setRow(i, patch)} />
                )}
              </div>
            );
          })}
        </div>
      )}

      {unmappedSrc.length > 0 && mappings.length > 0 && (
        <p className="mt-2 text-[11px] text-amber-700">
          ⚠ {unmappedSrc.length} source dataset{unmappedSrc.length === 1 ? "" : "s"} unmapped: {unmappedSrc.map((d) => d.name).join(", ")}
        </p>
      )}
    </div>
  );
}
