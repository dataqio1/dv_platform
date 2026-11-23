import { useEffect, useState } from "react";
import { api } from "../api";
import { fmtDateTime } from "../tz";

type LogLine = { ts: string; elapsed_ms: number; level: string; msg: string };

/** Collapsible execution trace for one compare run — phases, engine, timing, warnings and
 *  outcome — with a plain-text download. Lazily fetched on first expand. Pass `jobId` for a test
 *  case's latest run, or `runId` for a specific historical run. */
export function ExecutionLog({ jobId, runId, defaultOpen = false }: { jobId?: string; runId?: string; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const [lines, setLines] = useState<LogLine[] | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open || lines !== null) return;
    setLoading(true);
    const fetchLines = runId ? api.runLogs(runId) : api.compareLogs(jobId as string);
    fetchLines.then((r) => setLines(r.data)).catch(() => setLines([])).finally(() => setLoading(false));
  }, [open, jobId, runId, lines]);

  const color = (l: string) =>
    l === "error" ? "text-rose-400" : l === "warn" ? "text-amber-400" : "text-slate-300";

  return (
    <div className="rounded-xl border border-slate-200 bg-white">
      <div className="flex items-center justify-between px-5 py-3">
        <button onClick={() => setOpen((o) => !o)} className="flex items-center gap-2 text-sm font-semibold text-navy">
          <span className="text-slate-400">{open ? "▾" : "▸"}</span> Execution log
          {lines && <span className="text-xs font-normal text-slate-400">· {lines.length} lines</span>}
        </button>
        {jobId && !runId && (
          <a href={api.compareLogExportUrl(jobId)} download
            className="rounded-lg border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-600 hover:border-teal hover:text-teal-700">
            ↓ Download
          </a>
        )}
      </div>
      {open && (
        <div className="border-t border-slate-100">
          {loading ? (
            <p className="px-5 py-3 text-xs text-slate-400">Loading…</p>
          ) : !lines || lines.length === 0 ? (
            <p className="px-5 py-3 text-xs text-slate-400">No log recorded for this execution yet.</p>
          ) : (
            <div className="max-h-96 overflow-auto rounded-b-xl bg-slate-900 px-4 py-3 font-mono text-[11px] leading-relaxed">
              {lines.map((ln, i) => (
                <div key={i} className="whitespace-pre-wrap">
                  <span className="text-slate-500" title={fmtDateTime(ln.ts)}>+{(ln.elapsed_ms / 1000).toFixed(1)}s</span>{" "}
                  <span className={color(ln.level)}>{ln.msg}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
