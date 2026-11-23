import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { DetectionResult, DetectionRun, PatternSpec } from "../types";
import { Button } from "./ui";

/**
 * Bulk load-type detection as a scannable, progressive TABLE (not 100 cards). Kicks off a
 * background detection over the connection's scope and polls — rows stream in as each table is
 * classified, with a live progress bar. Each row's load type is editable; "Apply" hands the
 * resolved {table → load_pattern} map back to the create flow.
 */
export function LoadTypeDetectionTable({
  connectionId,
  database,
  schema,
  tables,
  patterns,
  onApply,
}: {
  connectionId: string;
  database?: string;
  schema?: string;
  tables: string[];
  patterns: PatternSpec[];
  onApply: (byTable: Record<string, string>) => void;
}) {
  const [run, setRun] = useState<DetectionRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [search, setSearch] = useState("");
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => () => { if (poll.current) clearInterval(poll.current); }, []);

  const start = async () => {
    setErr(null); setBusy(true); setRun(null); setOverrides({});
    try {
      const r = await api.startDetection(connectionId, { database, schema, tables });
      setRun(r.data);
      if (poll.current) clearInterval(poll.current);
      poll.current = setInterval(async () => {
        try {
          const cur = await api.getDetectionRun(r.data.id);
          setRun(cur.data);
          if (cur.data.status !== "running") {
            if (poll.current) clearInterval(poll.current);
            setBusy(false);
          }
        } catch { /* keep polling; transient */ }
      }, 1000);
    } catch (e) {
      setErr((e as Error).message); setBusy(false);
    }
  };

  const results = run?.results ?? [];
  const byTable: Record<string, DetectionResult> = {};
  for (const r of results) byTable[r.table_ref] = r;
  const patternOf = (ref: string) => overrides[ref] ?? byTable[ref]?.load_pattern ?? "";
  const label = (p: string) => patterns.find((x) => x.pattern === p)?.label ?? p;

  const shown = tables.filter((t) => !search.trim() || t.toLowerCase().includes(search.trim().toLowerCase()));
  const progress = run?.progress ?? { pct: 0, processed: 0, total: tables.length };
  const needAttention = results.filter((r) => r.load_pattern === "unknown").length;

  const apply = () => {
    const map: Record<string, string> = {};
    for (const t of tables) { const p = patternOf(t); if (p) map[t] = p; }
    onApply(map);
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={start} disabled={busy || tables.length === 0}>
          {busy ? "Detecting…" : run ? "Re-detect" : `Detect load types (${tables.length})`}
        </Button>
        {run && (
          <>
            <div className="h-2 w-48 overflow-hidden rounded-full bg-slate-100">
              <div className="h-full rounded-full bg-teal transition-all" style={{ width: `${progress.pct}%` }} />
            </div>
            <span className="text-xs text-slate-500">
              {progress.processed}/{progress.total} detected
              {needAttention > 0 ? ` · ${needAttention} need attention` : ""}
              {run.status === "error" ? " · failed" : ""}
            </span>
          </>
        )}
        {run && (
          <input
            value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Filter tables…"
            className="ml-auto w-48 rounded-lg border border-slate-300 px-2.5 py-1 text-xs outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
          />
        )}
      </div>
      {err && <p className="text-xs text-rose-600">{err}</p>}

      {run && (
        <div className="max-h-[520px] overflow-auto rounded-lg border border-slate-200">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-slate-50/95 text-left text-[11px] uppercase tracking-wide text-slate-400">
              <tr>
                <th className="px-3 py-2 font-semibold">Table</th>
                <th className="px-3 py-2 font-semibold">Status</th>
                <th className="px-3 py-2 font-semibold">Load type</th>
                <th className="px-3 py-2 font-semibold">Convention</th>
                <th className="px-3 py-2 font-semibold">Coverage</th>
                <th className="px-3 py-2 text-right font-semibold">Confidence</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((t) => {
                const r = byTable[t];
                const missing = r?.coverage?.missing_roles ?? [];
                return (
                  <tr key={t} className="border-t border-slate-100">
                    <td className="px-3 py-2 font-mono text-[13px] text-navy">{t}</td>
                    <td className="px-3 py-2">
                      {!r ? (
                        <span className="inline-flex items-center gap-1 text-xs text-slate-400">
                          <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-200 border-t-teal" /> detecting…
                        </span>
                      ) : r.load_pattern === "unknown" ? (
                        <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-700">unknown</span>
                      ) : (
                        <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-[11px] font-medium text-emerald-700">detected</span>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <select
                        value={patternOf(t)} disabled={!r}
                        onChange={(e) => setOverrides((o) => ({ ...o, [t]: e.target.value }))}
                        className="rounded-md border border-slate-200 bg-white px-1.5 py-1 text-xs outline-none focus:border-teal disabled:opacity-40"
                      >
                        <option value="">—</option>
                        {patterns.map((p) => <option key={p.pattern} value={p.pattern}>{label(p.pattern)}</option>)}
                      </select>
                    </td>
                    <td className="px-3 py-2 text-xs text-slate-500">{r?.matched_convention?.name ?? "—"}</td>
                    <td className="px-3 py-2 text-xs">
                      {missing.length ? (
                        <span className="text-amber-600" title={`missing: ${missing.join(", ")}`}>{missing.length} missing</span>
                      ) : r ? <span className="text-slate-400">—</span> : ""}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-xs text-slate-500">
                      {r?.confidence != null ? `${Math.round(r.confidence * 100)}%` : ""}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {run && run.status !== "running" && (
        <div className="flex justify-end">
          <Button onClick={apply}>Apply detected load types</Button>
        </div>
      )}
    </div>
  );
}
