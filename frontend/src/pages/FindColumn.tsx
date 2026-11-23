import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import type {
  Connection,
  FindColumnIndexStatus,
  FindColumnResult,
  FinderSearchSummary,
  Warning,
} from "../types";
import { Button, Card, EmptyState, Field, Spinner, TextInput } from "../components/ui";
import { Combobox } from "../components/Combobox";
import { Icon } from "../components/vex";

// Sentinel for the "search everything" scope (no connection/schema filter sent to the API).
const ALL = "__all__";

/**
 * Find Column — the Value Locator MVP. Paste one or more values, pick a connection +
 * schema, and see which tables/columns contain them, with confirmed sample rows.
 * With two or more values you can require them to co-occur in the SAME row (e.g. a zip
 * and a state) — the backend proves that with an exact conjunctive query.
 */
export default function FindColumn() {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [connectionId, setConnectionId] = useState("");
  const [schemas, setSchemas] = useState<string[]>([]);
  const [schema, setSchema] = useState("");
  const [values, setValues] = useState<string[]>([""]);
  const [coOccur, setCoOccur] = useState(true);  // multi-value defaults to "both in the same row"
  const [concurrency, setConcurrency] = useState(4);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<FindColumnResult | null>(null);
  const [warnings, setWarnings] = useState<Warning[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [index, setIndex] = useState<FindColumnIndexStatus | null>(null);
  const [indexing, setIndexing] = useState(false);
  const [schemasLoading, setSchemasLoading] = useState(false);
  const [schemaCachedAt, setSchemaCachedAt] = useState<string | null>(null);
  const [history, setHistory] = useState<FinderSearchSummary[]>([]);
  const [openingId, setOpeningId] = useState<string | null>(null);

  const loadHistory = useCallback(() => {
    api.finderSearches(50).then((r) => setHistory(r.data)).catch(() => setHistory([]));
  }, []);
  useEffect(() => loadHistory(), [loadHistory]);

  // Load connections once. Default to "All connections" unless there's exactly one.
  useEffect(() => {
    api.listConnections().then((r) => {
      setConnections(r.data);
      setConnectionId(r.data.length === 1 ? r.data[0].id : ALL);
    });
  }, []);

  const loadIndex = useCallback((cid: string) => {
    if (!cid || cid === ALL) {
      setIndex(null);
      return;
    }
    api.finderIndexStatus(cid).then((r) => setIndex(r.data)).catch(() => setIndex(null));
  }, []);

  const loadSchemas = useCallback((cid: string, refresh = false) => {
    if (!cid || cid === ALL) {
      setSchemas([]);
      setSchema(ALL);
      setSchemaCachedAt(null);
      return;
    }
    setSchemasLoading(true);
    api
      .listSchemas(cid, undefined, refresh)
      .then((r) => {
        setSchemas(r.data.schemas);
        setSchemaCachedAt(r.data.cached_at ?? null);
        // Default to a specific schema (fast path); "All schemas" is an explicit opt-in.
        setSchema((prev) =>
          prev !== ALL && r.data.schemas.includes(prev) ? prev : (r.data.schemas[0] ?? ALL),
        );
      })
      .catch(() => setSchemas([]))
      .finally(() => setSchemasLoading(false));
  }, []);

  // When a specific connection is chosen, fetch its schemas (cached) + index status.
  useEffect(() => {
    loadIndex(connectionId);
    loadSchemas(connectionId);
  }, [connectionId, loadIndex, loadSchemas]);

  async function refreshCatalog() {
    if (!connectionId || connectionId === ALL) return;
    setSchemasLoading(true);
    setError(null);
    try {
      const r = await api.refreshCatalogCache(connectionId);
      setSchemas(r.data.schemas);
      setSchemaCachedAt(r.data.cached_at ?? null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setSchemasLoading(false);
    }
  }

  async function buildIndex() {
    if (!connectionId || connectionId === ALL) return;
    setIndexing(true);
    setError(null);
    try {
      await api.buildFinderIndex({ connection_id: connectionId });
      loadIndex(connectionId);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setIndexing(false);
    }
  }

  async function toggleDaily(enabled: boolean) {
    if (!connectionId || connectionId === ALL) return;
    try {
      await api.setFinderIndexConfig({ connection_id: connectionId, frequency: "daily", enabled });
      loadIndex(connectionId);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  const cleanValues = useMemo(() => values.map((v) => v.trim()).filter(Boolean), [values]);
  const canSearch = !!connectionId && cleanValues.length > 0 && !busy;
  const multi = cleanValues.length >= 2;
  // Dates aren't supported — flag a date-looking value (YYYY-MM-DD[ T]HH:MM) so the user knows.
  const hasDateValue = cleanValues.some((v) => /^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2})?/.test(v));

  const setValueAt = (i: number, v: string) =>
    setValues((arr) => arr.map((x, idx) => (idx === i ? v : x)));
  const addValue = () => setValues((arr) => (arr.length >= 5 ? arr : [...arr, ""]));
  const removeValue = (i: number) =>
    setValues((arr) => (arr.length <= 1 ? arr : arr.filter((_, idx) => idx !== i)));

  async function run() {
    if (!canSearch) return;
    setBusy(true);
    setError(null);
    setResult(null);
    setWarnings([]);
    try {
      const r = await api.findColumn({
        connection_id: connectionId === ALL ? undefined : connectionId,
        values: cleanValues,
        schema: connectionId !== ALL && schema !== ALL ? schema : undefined,
        match_mode: coOccur && multi ? "co_occurrence" : "any",
        sample_limit: 20,
        confirm_concurrency: concurrency,
      });
      setResult(r.data);
      setWarnings(r.warnings);
      loadHistory();  // the just-run search now appears at the top of history
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function openSearch(id: string) {
    setOpeningId(id);
    setError(null);
    try {
      const r = await api.finderSearch(id);
      setResult(r.data.result);
      setWarnings([]);  // stored warnings live inside the result; the summary banners re-derive
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setOpeningId(null);
    }
  }

  async function deleteSearch(id: string) {
    setHistory((h) => h.filter((s) => s.id !== id));  // optimistic
    try {
      await api.deleteFinderSearch(id);
    } catch {
      loadHistory();  // restore on failure
    }
  }

  const connOptions = [
    ...connections.map((c) => ({ value: c.id, label: `${c.name} (${c.source_type})` })),
    { value: ALL, label: "All connections (slower)" },
  ];
  const schemaOptions = [
    ...schemas.map((s) => ({ value: s, label: s })),
    { value: ALL, label: "All schemas (slower)" },
  ];
  const allScope = connectionId === ALL;
  // Broad = no single schema pinned → scans every table of every schema/connection.
  const broadScope = allScope || schema === ALL;
  // The pre-search caution already covers broad scope, so drop the backend's broad_scope note
  // from the results; show truncation + "dates unsupported" as neutral notes, failures separately.
  const noteCodes = ["tables_truncated", "dates_unsupported"];
  const scopeNotes = warnings.filter((w) => noteCodes.includes(w.code));
  const scanIssues = warnings.filter((w) => !noteCodes.includes(w.code) && w.code !== "broad_scope");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-xl font-bold text-navy">
          <Icon name="search" className="h-5 w-5 text-teal-600" />
          Find Column
        </h1>
        <p className="mt-1 text-sm text-slate-500">
          Have a value but don’t know where it lives? Search a connection to see which tables and
          columns contain it — with real sample rows.
        </p>
      </div>

      <Card className="p-5">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Connection">
            <Combobox
              value={connectionId}
              onChange={setConnectionId}
              options={connOptions}
              placeholder="Pick a connection…"
              allowCustom={false}
            />
          </Field>
          <div>
            <div className="mb-1 flex items-center justify-between">
              <span className="text-sm font-medium text-slate-700">Schema</span>
              {!allScope && (
                <span className="flex items-center gap-2 text-[11px] text-slate-400">
                  {schemasLoading ? (
                    <span className="flex items-center gap-1">
                      <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-300 border-t-teal" />
                      loading…
                    </span>
                  ) : (
                    schemaCachedAt && <span>cached · {new Date(schemaCachedAt).toLocaleTimeString()}</span>
                  )}
                  <button
                    onClick={refreshCatalog}
                    disabled={schemasLoading}
                    className="inline-flex items-center gap-1 font-medium text-teal-700 hover:underline disabled:opacity-50"
                    title="Re-fetch schemas & tables from the source"
                  >
                    <Icon name="refresh" className={`h-3 w-3 ${schemasLoading ? "animate-spin" : ""}`} /> Refresh
                  </button>
                </span>
              )}
            </div>
            <Combobox
              value={allScope ? ALL : schema}
              onChange={setSchema}
              options={schemaOptions}
              placeholder={schemasLoading ? "Loading schemas…" : "All schemas"}
              allowCustom={false}
              disabled={allScope || schemasLoading}
            />
            <div className="mt-1 text-xs text-slate-400">
              {allScope ? "Every schema of every connection is scanned." : "Tables in this schema are scanned."}
            </div>
          </div>
        </div>

        <div className="mt-4">
          <div className="text-sm font-medium text-slate-700">Value(s) to find</div>
          <div className="mb-1.5 text-xs text-slate-400">
            Enter one value, or add more to find where they appear <span className="font-medium text-slate-500">together</span> —
            e.g. a state and a zip in the same row (US and 27519).
          </div>
          <div className="space-y-2">
            {values.map((v, i) => (
              <div key={i} className="flex items-center gap-2">
                <span className="w-5 shrink-0 text-right text-xs text-slate-400">{i + 1}.</span>
                <TextInput
                  value={v}
                  placeholder={i === 0 ? "e.g. 27519" : "e.g. US"}
                  onChange={(e) => setValueAt(i, e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") run();
                  }}
                />
                {values.length > 1 && (
                  <button
                    onClick={() => removeValue(i)}
                    className="shrink-0 rounded-lg px-2 py-2 text-slate-400 hover:bg-slate-50 hover:text-rose-600"
                    aria-label="Remove value"
                    title="Remove"
                  >
                    ✕
                  </button>
                )}
              </div>
            ))}
          </div>
          <div className="mt-2 flex items-center gap-3">
            {values.length < 5 ? (
              <Button variant="ghost" size="sm" onClick={addValue}>
                <Icon name="plus" className="h-3.5 w-3.5" /> Add value
              </Button>
            ) : (
              <span className="text-xs text-slate-400">Up to 5 values.</span>
            )}
          </div>
          <div className="mt-1.5 text-xs text-slate-400">
            Not indexed: date &amp; time columns, binary/LOB columns (CLOB, BLOB, …), and large
            free-text columns — so values in those won’t be found.
          </div>
          {hasDateValue && (
            <div className="mt-2 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              <span className="mt-0.5">⚠️</span>
              <div>That looks like a date. Find Column doesn’t search date/time columns, so it won’t match.</div>
            </div>
          )}
        </div>

        {multi && (
          <div className="mt-4">
            <div className="mb-1 text-sm font-medium text-slate-700">Where should the values appear?</div>
            <div className="inline-flex rounded-lg border border-slate-200 p-0.5 text-sm">
              {[
                { on: true, label: "Same row (both)" },
                { on: false, label: "Anywhere (either)" },
              ].map((opt) => (
                <button
                  key={String(opt.on)}
                  onClick={() => setCoOccur(opt.on)}
                  className={`rounded-md px-3 py-1.5 font-medium transition ${
                    coOccur === opt.on ? "bg-teal text-white" : "text-slate-600 hover:bg-slate-50"
                  }`}
                >
                  {opt.label}
                </button>
              ))}
            </div>
            <div className="mt-1 text-xs text-slate-400">
              {coOccur
                ? "Find tables where a single row contains all of these values (e.g. STATE=US and ZIP=27519 on the same row)."
                : "Find tables that contain any of these values (in any column)."}
            </div>
          </div>
        )}

        {broadScope && (
          <div className="mt-4 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            <span className="mt-0.5">⚠️</span>
            <div>
              {allScope
                ? "Searching every schema of every connection can be much slower."
                : "Searching all schemas scans every table in the connection and can be much slower."}{" "}
              For a fast search, pick a specific {allScope ? "connection and schema" : "schema"}
              {index?.indexed ? "" : " — or build an index below"}. Use “All” only as a last resort.
            </div>
          </div>
        )}

        <div className="mt-5 flex items-center gap-3">
          <Button onClick={run} disabled={!canSearch}>
            <Icon name="search" className="h-4 w-4" /> Find
          </Button>
          <label
            className="flex items-center gap-1.5 text-xs text-slate-500"
            title="How many candidate tables to confirm in parallel (concurrent source queries). Higher = faster broad searches, but more load on the source."
          >
            Parallel
            <input
              type="number"
              min={1}
              max={32}
              value={concurrency}
              onChange={(e) => setConcurrency(Math.max(1, Math.min(32, Number(e.target.value) || 1)))}
              className="w-14 rounded-md border border-slate-300 px-2 py-1 text-xs"
            />
          </label>
          {busy && <Spinner label={broadScope ? "Scanning every table — this can take a while…" : "Scanning tables…"} />}
        </div>

        {!allScope && (
          <div className="mt-5 flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-slate-100 pt-4 text-xs text-slate-500">
            {index?.indexed ? (
              <span className="inline-flex items-center gap-1 text-slate-600">
                <Icon name="dot" className="h-3 w-3 text-teal-500" />
                Indexed · {index.tables_indexed} table(s), {index.columns_indexed} column(s)
                {index.last_scanned_at ? ` · ${new Date(index.last_scanned_at).toLocaleString()}` : ""}
              </span>
            ) : (
              <span className="text-slate-400">
                No index yet — searches scan the source live. Build one to make searches instant.
              </span>
            )}
            <Button variant="ghost" size="sm" onClick={buildIndex} disabled={indexing}>
              <Icon name="refresh" className={`h-3.5 w-3.5 ${indexing ? "animate-spin" : ""}`} />
              {index?.indexed ? "Rebuild index" : "Build index"}
            </Button>
            <label className="inline-flex items-center gap-1.5">
              <input
                type="checkbox"
                className="accent-teal"
                checked={!!index?.config?.enabled && index?.config?.frequency === "daily"}
                onChange={(e) => toggleDaily(e.target.checked)}
              />
              Refresh daily
            </label>
          </div>
        )}
      </Card>

      {history.length > 0 && (
        <SearchHistory
          items={history}
          openingId={openingId}
          onOpen={openSearch}
          onDelete={deleteSearch}
        />
      )}

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div>
      )}

      {scopeNotes.length > 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          {scopeNotes.map((w, i) => (
            <div key={i}>{w.message}</div>
          ))}
        </div>
      )}

      {scanIssues.length > 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          <div className="font-medium">Some tables couldn’t be scanned:</div>
          <ul className="mt-1 list-disc pl-5">
            {scanIssues.map((w, i) => (
              <li key={i}>{w.message}</li>
            ))}
          </ul>
        </div>
      )}

      {result && <Results result={result} />}
    </div>
  );
}

function Results({ result }: { result: FindColumnResult }) {
  if (result.hits.length === 0) {
    return (
      <EmptyState
        title={`No matches in ${result.scanned_tables} table(s)`}
        hint={
          result.match_mode === "co_occurrence"
            ? "No single row contained all of those values. Try unchecking “same row”."
            : "The value wasn’t found in this schema. Try another connection or schema."
        }
      />
    );
  }
  const totalMatches = result.hits.reduce((sum, h) => sum + (h.match_count ?? 0), 0);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2 text-sm text-slate-500">
        <span>
          <span className="font-semibold text-navy">{totalMatches.toLocaleString()}</span> matching
          row(s) in <span className="font-semibold text-navy">{result.matched_tables}</span> of{" "}
          {result.scanned_tables} scanned table(s)
          {result.stats ? ` · ${result.stats.scanned_columns.toLocaleString()} column(s)` : ""} across{" "}
          {result.scanned_connections} connection(s)
          {result.match_mode === "co_occurrence" ? " · same-row co-occurrence" : ""}.
        </span>
        {/* Accuracy: the index is approximate, but every result is verified by an exact query. */}
        <span
          className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-inset ring-emerald-200"
          title="Results are exact. Approximate Bloom filters only shortlist candidate columns; every match is then confirmed against the source with an exact query, so there are no false positives."
        >
          ✓ exact (verified)
        </span>
        {result.used_index && (
          <span
            className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700 ring-1 ring-inset ring-amber-200"
            title="Candidate columns were pruned by shape metadata, confirmed via exact set or Bloom, then verified with a live query."
          >
            ⚡ used index
          </span>
        )}
        {result.stats && (
          <span
            className="text-xs text-slate-400"
            title={`total ${result.stats.took_ms}ms · shortlist ${result.stats.shortlist_ms}ms · confirm ${result.stats.confirm_ms}ms · ${result.stats.blooms_loaded} Bloom(s) loaded · ${result.stats.tables_confirmed} table(s) confirmed`}
          >
            took {result.stats.took_ms}ms
          </span>
        )}
      </div>
      {result.hits.map((hit) => (
        <Card key={`${hit.connection_id}:${hit.schema}.${hit.table}`} className="p-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="font-semibold text-navy">
              <span className="text-slate-400">{hit.connection_name} / </span>
              {hit.schema}.<span className="text-teal-700">{hit.table}</span>
            </div>
            <div className="text-xs text-slate-400">
              {hit.match_count != null ? `${hit.match_count.toLocaleString()} matching row(s)` : ""}
            </div>
          </div>

          <div className="mt-2 flex flex-wrap gap-1.5">
            {hit.columns.map((c, i) => (
              <span
                key={i}
                className="inline-flex items-center gap-1 rounded-full bg-teal/10 px-2.5 py-0.5 text-xs font-medium text-teal-700"
                title={`${c.matches.toLocaleString()} row(s)`}
              >
                <span className="font-semibold">{c.column}</span>
                <span className="text-teal-600/70">=</span>
                <span className="font-mono">{c.value}</span>
              </span>
            ))}
          </div>

          <SampleTable sample={hit.sample} matchedColumns={new Set(hit.columns.map((c) => c.column))} />
        </Card>
      ))}
    </div>
  );
}

function SampleTable({
  sample,
  matchedColumns,
}: {
  sample: { columns: string[]; rows: unknown[][] };
  matchedColumns: Set<string>;
}) {
  if (!sample.rows.length) return <div className="mt-3 text-xs text-slate-400">No sample rows available.</div>;
  return (
    <div className="mt-3 overflow-x-auto rounded-lg border border-slate-200">
      <table className="min-w-full text-left text-sm">
        <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            {sample.columns.map((c) => (
              <th
                key={c}
                className={`whitespace-nowrap px-3 py-2 font-semibold ${
                  matchedColumns.has(c) ? "text-teal-700" : ""
                }`}
              >
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {sample.rows.map((row, ri) => (
            <tr key={ri} className="hover:bg-slate-50/50">
              {row.map((cell, ci) => (
                <td
                  key={ci}
                  className={`whitespace-nowrap px-3 py-1.5 font-mono text-xs ${
                    matchedColumns.has(sample.columns[ci]) ? "text-navy" : "text-slate-500"
                  }`}
                >
                  {cell === null || cell === undefined ? (
                    <span className="text-slate-300">NULL</span>
                  ) : (
                    String(cell)
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function timeAgo(iso: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(iso).toLocaleDateString();
}

/** Per-user list of past searches with scan/latency metadata — click to re-open, trash to delete. */
function SearchHistory({
  items,
  openingId,
  onOpen,
  onDelete,
}: {
  items: FinderSearchSummary[];
  openingId: string | null;
  onOpen: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <Card className="p-5">
      <div className="mb-3 flex items-center gap-2">
        <Icon name="search" className="h-4 w-4 text-slate-400" />
        <h2 className="text-sm font-semibold text-navy">Recent searches</h2>
        <span className="text-xs text-slate-400">({items.length})</span>
      </div>
      <ul className="divide-y divide-slate-100">
        {items.map((s) => (
          <li key={s.id} className="group flex items-center gap-3 py-2.5">
            <button
              onClick={() => onOpen(s.id)}
              disabled={openingId === s.id}
              className="min-w-0 flex-1 text-left"
              title="Re-open this search"
            >
              <div className="flex flex-wrap items-center gap-1.5">
                {s.values.map((v, i) => (
                  <span
                    key={i}
                    className="inline-flex items-center rounded-full bg-teal/10 px-2 py-0.5 font-mono text-xs font-medium text-teal-700"
                  >
                    {v}
                  </span>
                ))}
                {s.match_mode === "co_occurrence" && s.values.length > 1 && (
                  <span className="text-[11px] text-slate-400">· same row</span>
                )}
                {openingId === s.id && (
                  <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-300 border-t-teal" />
                )}
              </div>
              <div className="mt-0.5 truncate text-xs text-slate-500">
                <span className="text-slate-600">{s.scope_label}</span>
                {" · "}
                <span className="font-medium text-navy">{s.total_matches.toLocaleString()}</span> match(es) ·
                scanned {s.scanned_tables.toLocaleString()} table(s), {s.scanned_columns.toLocaleString()} column(s) ·
                took {(s.took_ms / 1000).toFixed(1)}s · {timeAgo(s.created_at)}
              </div>
            </button>
            <button
              onClick={() => onDelete(s.id)}
              className="shrink-0 rounded-lg p-2 text-slate-300 hover:bg-rose-50 hover:text-rose-600"
              aria-label="Delete search"
              title="Delete this search"
            >
              <Icon name="trash" className="h-4 w-4" />
            </button>
          </li>
        ))}
      </ul>
    </Card>
  );
}
