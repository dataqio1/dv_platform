import { useCallback, useEffect, useState } from "react";
import { useParams, Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { usePrefs } from "../prefs";
import type { Bucket, CompareJob, DiffRow, FailureTrend, Project, SchemaRec } from "../types";
import { Button, Card, Field, HealthBadge, Modal, Spinner, TextInput } from "../components/ui";
import { SortTh, useSort } from "../components/sortable";
import { ExecutionLog } from "../components/ExecutionLog";
import { StatRing } from "../components/viz";
import { ScheduledIcon } from "../components/ScheduleControl";

/** A tiny inline SVG sparkline of failed-record counts over the runs of one parameter set. */
function Sparkline({ values, width = 120, height = 28 }: { values: number[]; width?: number; height?: number }) {
  if (values.length < 2) return null;
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const step = width / (values.length - 1);
  const pts = values.map((v, i) => `${(i * step).toFixed(1)},${(height - ((v - min) / span) * height).toFixed(1)}`);
  return (
    <svg width={width} height={height} className="block">
      <polyline points={pts.join(" ")} fill="none" stroke="currentColor" strokeWidth="1.5" />
      {values.map((v, i) => (
        <circle key={i} cx={i * step} cy={height - ((v - min) / span) * height} r={i === values.length - 1 ? 2.5 : 1.5} fill="currentColor" />
      ))}
    </svg>
  );
}

/** The +/- failure-growth badge: change in failed records vs the previous run (same parameters),
 *  with a hover sparkline of the actual values. Up = more failures (red); down = fewer (green). */
function FailureTrendBadge({ trend }: { trend: FailureTrend | null }) {
  if (!trend || trend.current === null) return null;
  const { direction, pct_change, abs_delta, points } = trend;
  const values = points.map((p) => p.failed_count ?? 0);
  const tone =
    direction === "up" ? "text-rose-600 bg-rose-50"
    : direction === "down" ? "text-emerald-600 bg-emerald-50"
    : "text-slate-500 bg-slate-100";
  const arrow = direction === "up" ? "▲" : direction === "down" ? "▼" : "→";
  const label =
    abs_delta === null ? "first run"
    : pct_change !== null ? `${pct_change > 0 ? "+" : ""}${pct_change}%`
    : `${abs_delta > 0 ? "+" : ""}${abs_delta}`;
  return (
    <span className="group relative inline-flex items-center gap-1" title={`${trend.current} failed records now`}>
      <span className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium ${tone}`}>
        <span className="text-[10px]">{arrow}</span>{label}
      </span>
      {values.length >= 2 && (
        <span className={`pointer-events-none absolute left-0 top-full z-10 mt-1 hidden rounded-lg border border-slate-200 bg-white p-2 shadow-lg group-hover:block ${direction === "up" ? "text-rose-500" : direction === "down" ? "text-emerald-500" : "text-slate-400"}`}>
          <Sparkline values={values} />
          <span className="mt-1 block whitespace-nowrap text-[10px] text-slate-500">
            failed records · {values[values.length - 2]} → <strong>{values[values.length - 1]}</strong> over {values.length} runs
          </span>
        </span>
      )}
    </span>
  );
}

/** Shows the run-time key-detection outcome so a user who submitted without a key sees what the
 *  engine found (and when), or that none could be detected. */
function KeyDetectionNote({ job }: { job: CompareJob }) {
  if (job.kind !== "data" || !job.key_status) return null;
  const when = job.key_detected_at ? new Date(job.key_detected_at).toLocaleString() : null;
  const keys = (job.detected_key ?? []).join(", ");
  if (job.key_status === "not_detected") {
    return (
      <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-700">
        <span className="font-semibold">⚠ No key detected{when ? ` in last run on ${when}` : ""}.</span>{" "}
        A data compare can’t line up rows without one — add a key in the Mapping step and re-run.
      </div>
    );
  }
  const tone = job.key_status === "user" ? "border-slate-200 bg-slate-50 text-slate-600" : "border-emerald-200 bg-emerald-50 text-emerald-700";
  const verb = job.key_status === "user" ? "Key provided" : `Key detected${when ? ` in last run on ${when}` : ""}`;
  return (
    <div className={`rounded-lg border px-3 py-2 text-xs ${tone}`}>
      🔑 <span className="font-semibold">{verb}:</span> {keys || "—"}
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  // Matches the Figma KpiCard look (vex.tsx): big tabular number, muted medium label.
  return (
    <Card className="px-4 py-3.5">
      <div className="text-xs font-medium text-slate-500">{label}</div>
      <div className={`mt-1 text-3xl font-bold tabular-nums ${tone ?? "text-navy"}`}>{value}</div>
    </Card>
  );
}

function CountView({ s, job }: { s: Record<string, number>; job: CompareJob }) {
  const tol = (job.options?.tolerance_pct as number) ?? 0;
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label="Source rows" value={s.src_count?.toLocaleString()} />
        <Stat label="Target rows" value={s.dst_count?.toLocaleString()} />
        <Stat
          label="Difference"
          value={s.diff?.toLocaleString()}
          tone={s.match ? "text-emerald-600" : "text-rose-600"}
        />
        <Stat label="Diff %" value={`${s.diff_pct ?? 0}%`} />
      </div>
      <div className="text-xs text-slate-400">
        Tolerance: {tol}% · {s.match ? "within tolerance ✓" : "exceeds tolerance ✗"}
      </div>
    </div>
  );
}

const BUCKETS: { key: Bucket; label: string; tone: string }[] = [
  { key: "match", label: "Passed", tone: "text-emerald-600" },
  { key: "value_difference", label: "Value diff (1)", tone: "text-amber-600" },
  { key: "source_only", label: "Source only (2)", tone: "text-sky-600" },
  { key: "target_only", label: "Target only (3)", tone: "text-violet-600" },
  { key: "ambiguous", label: "Ambiguous (4)", tone: "text-slate-500" },
];

function keyStr(k: Record<string, unknown>) {
  return Object.entries(k)
    .map(([kk, v]) => `${kk}=${String(v)}`)
    .join(", ");
}

function RecordTable({ rows, extraCols, showExtra }: { rows: DiffRow[]; extraCols: Set<string>; showExtra: boolean }) {
  if (rows.length === 0) return <div className="py-6 text-center text-xs text-slate-400">None.</div>;
  const allCols = Object.keys(rows[0].record ?? {});
  const cols = showExtra ? allCols : allCols.filter((c) => !extraCols.has(c));
  const isExtra = (c: string) => extraCols.has(c);
  return (
    <table className="w-full text-xs">
      <thead className="border-b border-slate-200 text-left text-slate-400">
        <tr>
          {cols.map((c) => (
            <th key={c} className={`px-2 py-1 font-medium ${isExtra(c) ? "bg-slate-50 text-slate-400" : ""}`}>
              {c}
              {isExtra(c) && <span className="ml-1 rounded bg-slate-200 px-1 py-px text-[9px] font-semibold uppercase tracking-wide text-slate-500">not compared</span>}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i} className="border-b border-slate-50 last:border-0">
            {cols.map((c) => (
              <td key={c} className={`px-2 py-1 font-mono ${isExtra(c) ? "bg-slate-50/60 italic text-slate-400" : "text-slate-600"}`}>
                {String(r.record?.[c] ?? "")}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ValueDiffList({ rows, extraCols, showExtra, systematic }: { rows: DiffRow[]; extraCols: Set<string>; showExtra: boolean; systematic?: Set<string> }) {
  const sys = systematic ?? new Set<string>();
  const [onlyDiffCols, setOnlyDiffCols] = useState(false);
  if (rows.length === 0) return <div className="py-6 text-center text-xs text-slate-400">None.</div>;

  const keyCols = Object.keys(rows[0].key);
  // A column is "extra" (not compared) if its cell is flagged compared===false (fallback: in the
  // summary's extra set). Compared columns drive the diff; extra ones only ride along for context.
  const cells0 = rows[0].cells ?? [];
  const extraSet = new Set<string>(
    cells0.filter((c) => c.compared === false || extraCols.has(c.column)).map((c) => c.column),
  );
  let comparedCols = cells0.map((c) => c.column).filter((c) => !extraSet.has(c));
  if (onlyDiffCols) {
    const diffSet = new Set<string>();
    rows.forEach((r) => (r.cells ?? []).forEach((c) => !extraSet.has(c.column) && c.differs !== false && diffSet.add(c.column)));
    comparedCols = comparedCols.filter((c) => diffSet.has(c));
  }
  const extraColNames = showExtra ? cells0.map((c) => c.column).filter((c) => extraSet.has(c)) : [];
  const valCols = [...comparedCols, ...extraColNames];

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-xs text-slate-400">
        <span>
          <span className="mr-1 inline-block h-2 w-2 rounded-sm bg-amber-200 align-middle" /> differing cell · source above, target below
        </span>
        <label className="flex cursor-pointer items-center gap-1.5">
          <input type="checkbox" checked={onlyDiffCols} onChange={(e) => setOnlyDiffCols(e.target.checked)} />
          only differing columns ({comparedCols.length} shown)
        </label>
      </div>
      <div className="overflow-x-auto rounded-lg border border-slate-200">
        <table className="min-w-full border-collapse text-xs">
          <thead>
            <tr className="bg-slate-50">
              {keyCols.map((k) => (
                <th key={k} className="sticky left-0 z-10 border-b border-r border-slate-200 bg-slate-50 px-2 py-1.5 text-left font-semibold text-navy">
                  {k}
                </th>
              ))}
              {valCols.map((c) => (
                <th key={c} className={`whitespace-nowrap border-b border-slate-200 px-2 py-1.5 text-left font-medium ${sys.has(c) ? "bg-rose-50 text-rose-700" : extraSet.has(c) ? "bg-slate-100/70 text-slate-400" : "text-slate-500"}`}>
                  {sys.has(c) && <span className="mr-1" title="systematic difference">🔴</span>}{c}
                  {sys.has(c) && <span className="ml-1 rounded bg-rose-100 px-1 py-px text-[9px] font-semibold uppercase tracking-wide text-rose-700">~100% diff</span>}
                  {!sys.has(c) && extraSet.has(c) && <span className="ml-1 rounded bg-slate-200 px-1 py-px text-[9px] font-semibold uppercase tracking-wide text-slate-500">not compared</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((d, i) => {
              const byCol: Record<string, { src: unknown; dst: unknown; differs?: boolean }> = {};
              (d.cells ?? []).forEach((c) => (byCol[c.column] = c));
              return (
                <tr key={i} className="odd:bg-white even:bg-slate-50/40">
                  {keyCols.map((k) => (
                    <td key={k} className="sticky left-0 z-10 border-r border-slate-200 bg-inherit px-2 py-1 font-mono font-medium text-slate-700">
                      {String(d.key[k])}
                    </td>
                  ))}
                  {valCols.map((col) => {
                    const c = byCol[col];
                    if (extraSet.has(col)) {
                      // Context-only column: show the value(s) muted, never judged as a diff.
                      const bothDiffer = String(c?.src ?? "") !== String(c?.dst ?? "");
                      return (
                        <td key={col} className="whitespace-nowrap bg-slate-50/60 px-2 py-1 font-mono italic text-slate-400">
                          {bothDiffer ? (
                            <div className="leading-tight">
                              <div>{String(c?.src ?? "")}</div>
                              <div className="text-slate-400">{String(c?.dst ?? "")}</div>
                            </div>
                          ) : (
                            String(c?.src ?? "")
                          )}
                        </td>
                      );
                    }
                    const diff = c?.differs !== false;
                    return (
                      <td key={col} className={`whitespace-nowrap px-2 py-1 font-mono ${diff ? "bg-amber-50" : ""}`}>
                        {diff ? (
                          <div className="leading-tight">
                            <div className="text-rose-600 line-through decoration-rose-300">{String(c?.src)}</div>
                            <div className="text-emerald-700">{String(c?.dst)}</div>
                          </div>
                        ) : (
                          <span className="text-slate-500">{String(c?.src)}</span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** When there are too many differing rows to eyeball, show WHICH columns drive the differences —
 *  a count + bar per column from the summary's per_column_mismatch. The actionable view at scale. */
function PerColumnBreakdown({ perCol, valueDiffRows, systematic }: { perCol?: Record<string, number>; valueDiffRows: number; systematic?: Set<string> }) {
  const sys = systematic ?? new Set<string>();
  // Include systematic columns even if they're not in per_column_mismatch (they may be flagged by
  // the sample gate without a full-table count).
  const cols = new Map<string, number>(Object.entries(perCol ?? {}));
  for (const c of sys) if (!cols.has(c)) cols.set(c, valueDiffRows || 1);
  if (cols.size === 0) return null;
  const entries = [...cols.entries()].sort((a, b) => (sys.has(b[0]) ? 1 : 0) - (sys.has(a[0]) ? 1 : 0) || b[1] - a[1]);
  const max = Math.max(...entries.map((e) => e[1]), 1);
  return (
    <Card className="p-4">
      <div className="mb-1 text-sm font-semibold text-navy">Where values differ <span className="font-normal text-slate-400">— by column</span></div>
      <p className="mb-3 text-xs text-slate-500">
        {valueDiffRows.toLocaleString()} rows differ — too many to read one by one. This shows which columns drive the differences, so you know where to look.
      </p>
      <div className="space-y-1.5">
        {entries.map(([col, n]) => {
          const isSys = sys.has(col);
          return (
            <div key={col} className="flex items-center gap-2 text-xs">
              <span className={`w-40 shrink-0 truncate font-mono ${isSys ? "font-semibold text-rose-700" : "text-slate-700"}`} title={col}>
                {isSys && <span className="mr-1" title="systematic difference">🔴</span>}{col}
              </span>
              <div className="h-3 flex-1 overflow-hidden rounded bg-slate-100">
                <div className={`h-full rounded ${isSys ? "bg-rose-500" : "bg-amber-400"}`} style={{ width: `${Math.max(2, (n / max) * 100)}%` }} />
              </div>
              <span className={`w-24 shrink-0 text-right font-medium ${isSys ? "text-rose-700" : "text-amber-700"}`}>
                {isSys ? "~100%" : `${n.toLocaleString()} rows`}
              </span>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

/** Result of a sample dry-run: a verdict + the same counts/per-column breakdown as a full run,
 *  but computed on ~1,000 sampled rows and never persisted. */
function DryRunView({ loading, err, data }: {
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
  return (
    <div className="space-y-3">
      <div className={`rounded-lg px-3 py-2 text-sm ${clean ? "border border-emerald-300 bg-emerald-50 text-emerald-800" : "border border-amber-300 bg-amber-50 text-amber-800"}`}>
        {clean ? "✓ " : "⚠ "}Dry-run on <span className="font-semibold">{data.sampled.toLocaleString()}</span> sample rows — <span className="font-semibold">{Math.round(rate)}%</span> match.
        {clean ? " The key and mapping line up cleanly — safe to run the full test." : " Differences found on the sample — review the key/mapping/tolerances before the full run."}
      </div>
      {data.warnings?.map((w, i) => <p key={i} className="rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-700">{w}</p>)}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Matched" value={matched.toLocaleString()} tone="text-emerald-600" />
        <Stat label="Value diff" value={vdiff.toLocaleString()} tone="text-amber-600" />
        <Stat label="Source only" value={srcOnly.toLocaleString()} tone="text-sky-600" />
        <Stat label="Target only" value={tgtOnly.toLocaleString()} tone="text-violet-600" />
      </div>
      <PerColumnBreakdown perCol={(s as Record<string, unknown>).per_column_mismatch as Record<string, number> | undefined} valueDiffRows={vdiff} />
      <p className="text-[11px] text-slate-400">This was a bounded sample, not the full table — run the test for the complete result.</p>
    </div>
  );
}

function DataView({ s, jobId }: { s: Record<string, number>; jobId: string }) {
  const [tab, setTab] = useState<Bucket>("value_difference");
  const [rows, setRows] = useState<DiffRow[] | null>(null);
  const engine = (s as Record<string, unknown>).engine as string | undefined;
  // Non-compared columns captured for context (when the compare opted in). Toggle their visibility.
  const extraCols = new Set<string>(((s as Record<string, unknown>).extra_columns as string[]) ?? []);
  // Columns the Stage-0 sample gate found ~100% different (systematic format/timezone/encoding issue).
  const systematicCols = new Set<string>(((s as Record<string, unknown>).systematic_columns as string[]) ?? []);
  const [showExtra, setShowExtra] = useState(false);

  const count: Record<Bucket, number> = {
    match: s.matched_rows ?? 0,
    value_difference: s.value_difference_rows ?? s.mismatched_rows ?? 0,
    source_only: s.source_only_rows ?? 0,
    target_only: s.target_only_rows ?? 0,
    ambiguous: s.ambiguous_rows ?? 0,
  };

  const load = useCallback(() => {
    setRows(null);
    api.compareDifferences(jobId, tab).then((r) => setRows(r.data as DiffRow[]));
  }, [jobId, tab]);
  useEffect(() => {
    load();
  }, [load]);

  // How many rows were actually compared vs how many the tables hold. A sampled short-circuit
  // (hash_sample) or a fail-fast stop means only a fraction was checked — surface that plainly
  // so "0% match" on a 30-row sample isn't mistaken for a full-table result.
  const recordsCompared = s.keys_total ?? 0;
  const totalRows = Math.max(s.src_row_count ?? 0, s.dst_row_count ?? 0);
  const sampled = engine === "hash_sample";
  const stoppedEarly = !!(s as Record<string, unknown>).stopped_early;
  const partial = (sampled || stoppedEarly) && totalRows > recordsCompared;

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-stretch">
        <Card className="flex items-center gap-4 p-4 sm:w-60">
          <StatRing pct={(s.match_rate_pct as number) ?? 0} />
          <div>
            <div className="text-xs uppercase tracking-wide text-slate-400">Match rate</div>
            <div className="mt-0.5 text-[11px] text-slate-500">{count.match.toLocaleString()} matched</div>
            <div className="text-[11px] text-slate-400">of {(s.keys_total ?? 0).toLocaleString()} rows compared</div>
          </div>
        </Card>
        <div className="grid flex-1 grid-cols-2 gap-4 sm:grid-cols-4">
          <Stat label="Passed" value={count.match.toLocaleString()} tone="text-emerald-600" />
          <Stat label="Value diff" value={count.value_difference.toLocaleString()} tone="text-amber-600" />
          <Stat label="Source only" value={count.source_only.toLocaleString()} tone="text-sky-600" />
          <Stat label="Target only" value={count.target_only.toLocaleString()} tone="text-violet-600" />
        </div>
      </div>

      <div className={`flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg px-3 py-2 text-xs ${
        partial ? "border border-amber-300 bg-amber-50 text-amber-800" : "text-slate-500"}`}>
        <span>
          {partial ? "⚠ " : ""}
          <span className="font-semibold">
            {recordsCompared.toLocaleString()}{totalRows > recordsCompared ? ` of ${totalRows.toLocaleString()}` : ""}
          </span>{" "}
          row{recordsCompared === 1 ? "" : "s"} compared
        </span>
        {sampled && (
          <span>· fail-fast: a random sample showed heavy mismatch, so the run exited after confirming the failures — the full table was <strong>not</strong> scanned. Re-run a full compare (engine <code className="font-mono">hash_force</code>) to check every row.</span>
        )}
        {!sampled && stoppedEarly && <span>· stopped early (fail-fast) — this is a lower bound, not a full compare.</span>}
      </div>

      {systematicCols.size > 0 && (
        <div className="rounded-lg border border-rose-300 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <span className="font-semibold">⚠ Systematic difference</span> — {" "}
          {[...systematicCols].map((c) => (
            <span key={c} className="mx-0.5 inline-block rounded bg-rose-100 px-1.5 py-0.5 font-mono text-xs font-semibold text-rose-700">{c}</span>
          ))}
          {" "}differ in ~100% of sampled rows. This usually means a <b>format / timezone / encoding mismatch</b>
          (e.g. a date rendered differently on each engine), not scattered data drift — check the column’s type/format before trusting the row counts.
        </div>
      )}

      <PerColumnBreakdown perCol={(s as Record<string, unknown>).per_column_mismatch as Record<string, number> | undefined}
        valueDiffRows={count.value_difference} systematic={systematicCols} />

      <Card className="p-5">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          {BUCKETS.map((b) => (
            <button
              key={b.key}
              onClick={() => setTab(b.key)}
              className={`rounded-full px-3 py-1 text-xs font-medium ${
                tab === b.key ? "bg-navy text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"
              }`}
            >
              {b.label} <span className="opacity-70">{count[b.key]}</span>
            </button>
          ))}
          {extraCols.size > 0 && (
            <label className="ml-auto flex cursor-pointer items-center gap-1.5 text-xs text-slate-500" title="Show the columns that were captured for context but not part of the compare">
              <input type="checkbox" checked={showExtra} onChange={(e) => setShowExtra(e.target.checked)} />
              Show extra columns <span className="rounded bg-slate-200 px-1 py-px text-[9px] font-semibold uppercase tracking-wide text-slate-500">not compared</span>
              <span className="text-slate-400">({extraCols.size})</span>
            </label>
          )}
          <a
            href={api.differencesCsvUrl(jobId)}
            download
            className={`inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-600 hover:border-teal hover:text-teal-700 ${extraCols.size > 0 ? "" : "ml-auto"}`}
            title="Download all captured differences as CSV"
          >
            ↓ Download CSV
          </a>
        </div>
        {rows !== null && rows.length < count[tab] && (
          <div className="mb-3 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            ⚠ Showing a sample of <span className="font-semibold">{rows.length.toLocaleString()}</span> of{" "}
            <span className="font-semibold">{count[tab].toLocaleString()}</span> {BUCKETS.find((b) => b.key === tab)?.label.toLowerCase()} —
            the engine captures a bounded sample per bucket, so the remaining rows aren&apos;t stored. Use the counts above
            (and the per-column breakdown) for the full picture, or <a href={api.differencesCsvUrl(jobId)} download className="font-medium underline">download the captured sample as CSV</a>.
          </div>
        )}
        {rows === null ? (
          <Spinner />
        ) : tab === "value_difference" ? (
          <ValueDiffList rows={rows} extraCols={extraCols} showExtra={showExtra} systematic={systematicCols} />
        ) : tab === "ambiguous" ? (
          rows.length === 0 ? (
            <div className="py-6 text-center text-xs text-slate-400">None.</div>
          ) : (
            <div className="space-y-1 text-xs">
              {rows.map((r, i) => (
                <div key={i} className="font-mono text-slate-600">
                  {keyStr(r.key)} — source×{r.src_count}, target×{r.dst_count} (set aside)
                </div>
              ))}
            </div>
          )
        ) : (
          <RecordTable rows={rows} extraCols={extraCols} showExtra={showExtra} />
        )}
        <div className="mt-2 flex items-center justify-between text-[11px] text-slate-400">
          <span>{rows && rows.length < count[tab] ? `Sample of ${rows.length.toLocaleString()} of ${count[tab].toLocaleString()}.` : rows ? `Showing all ${rows.length.toLocaleString()}.` : ""}</span>
          {engine && (
            <span className="rounded-full bg-slate-100 px-2 py-0.5 font-medium text-slate-500">
              engine:{" "}
              {engine === "hash"
                ? "hash pushdown"
                : engine === "recursive_hash"
                  ? "recursive hash (in-database)"
                  : engine === "fingerprint"
                    ? "fingerprint (cross-DB checksum + bisection)"
                    : engine === "streaming"
                      ? "streaming (bounded key-range)"
                      : engine}
            </span>
          )}
        </div>
      </Card>
    </div>
  );
}

function SchemaView({ s, recs }: { s: Record<string, number>; recs: SchemaRec[] }) {
  const { sorted, sort, requestSort } = useSort(recs, {
    column: (r) => r.column,
    src_type: (r) => r.src_type,
    dst_type: (r) => r.dst_type,
    status: (r) => r.status,
  });
  const tone: Record<string, string> = {
    match: "text-emerald-600",
    type_mismatch: "text-amber-600",
    source_only: "text-sky-600",
    target_only: "text-violet-600",
  };
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label="Columns" value={s.columns_total} />
        <Stat label="Type mismatches" value={s.type_mismatches} tone="text-amber-600" />
        <Stat label="Source only" value={s.source_only} tone="text-sky-600" />
        <Stat label="Target only" value={s.target_only} tone="text-violet-600" />
      </div>
      <Card className="p-5">
        <table className="w-full text-sm">
          <thead className="border-b border-slate-200 bg-slate-50/60 text-left text-[11px] uppercase tracking-wide text-slate-400">
            <tr>
              <SortTh label="Column" columnKey="column" sort={sort} onSort={requestSort} className="px-3 py-2 font-medium" />
              <SortTh label="Source type" columnKey="src_type" sort={sort} onSort={requestSort} className="px-3 py-2 font-medium" />
              <SortTh label="Target type" columnKey="dst_type" sort={sort} onSort={requestSort} className="px-3 py-2 font-medium" />
              <SortTh label="Status" columnKey="status" sort={sort} onSort={requestSort} className="px-3 py-2 font-medium" />
            </tr>
          </thead>
          <tbody>
            {sorted.map((r) => (
              <tr key={r.column} className="border-b border-slate-50 last:border-0">
                <td className="px-3 py-2 font-medium text-navy">{r.column}</td>
                <td className="px-3 py-2 text-slate-500">{r.src_type ?? "—"}</td>
                <td className="px-3 py-2 text-slate-500">{r.dst_type ?? "—"}</td>
                <td className={`px-3 py-2 font-medium ${tone[r.status] ?? "text-slate-500"}`}>
                  {r.status}
                  {r.src_type && r.dst_type && r.canonical_match && r.status !== "match"
                    ? " (canonical ✓)"
                    : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

export default function CompareDetail() {
  const { jobId = "" } = useParams();
  const navigate = useNavigate();
  const { recordRecent, loaded: prefsLoaded } = usePrefs();
  const [job, setJob] = useState<CompareJob | null>(null);
  const [schemaRecs, setSchemaRecs] = useState<SchemaRec[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Runtime variables this test case references, and the values to run it with.
  const [reqVars, setReqVars] = useState<string[]>([]);
  const [varVals, setVarVals] = useState<Record<string, string>>({});
  const [running, setRunning] = useState(false);
  // Dry-run: run the real compare on a small sample, no persistence — a cheap pre-run sanity check.
  const [dryOpen, setDryOpen] = useState(false);
  const [dryLoading, setDryLoading] = useState(false);
  const [dryErr, setDryErr] = useState<string | null>(null);
  const [dry, setDry] = useState<{ sampled: number; summary: Record<string, number>; diffs: unknown[]; warnings: string[] } | null>(null);

  const [trend, setTrend] = useState<FailureTrend | null>(null);
  const load = useCallback(() => {
    api.getCompare(jobId).then((r) => setJob(r.data)).catch((e) => setErr(e.message));
    // Failure-growth vs the previous run of the SAME parameter set (latest set by default).
    api.failureTrend(jobId).then((r) => setTrend(r.data)).catch(() => setTrend(null));
  }, [jobId]);
  useEffect(() => { load(); }, [load]);
  // Remember this test case in "recently viewed" (once prefs are loaded, so we don't clobber the list).
  useEffect(() => {
    if (job && prefsLoaded) recordRecent({ kind: "Test case", id: job.id, name: job.name || job.id.slice(0, 8), path: `/validation/${job.id}` });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.id, job?.name, prefsLoaded]);

  // What {{vars}} does this test reference? (table/sql/schema/db/filter/key)
  useEffect(() => {
    api.jobVariables(jobId).then((r) => setReqVars(r.data)).catch(() => setReqVars([]));
  }, [jobId]);
  // Projects this test case can be moved into.
  const [projects, setProjects] = useState<Project[]>([]);
  useEffect(() => {
    api.listProjects().then((r) => setProjects(r.data)).catch(() => setProjects([]));
  }, []);

  const onAssignProject = async (projectId: string) => {
    try {
      await api.assignCompare(jobId, { project_id: projectId || null });
      load();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  // Seed the inputs with the values from the last run, so re-running is one click.
  useEffect(() => {
    const used = job?.result?.variables;
    if (used && Object.keys(used).length) setVarVals((p) => ({ ...used, ...p }));
  }, [job?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const varsReady = reqVars.every((v) => (varVals[v] ?? "").trim());

  const opts = (job?.options ?? {}) as Record<string, unknown>;
  const dryKey = (opts.key as string[]) ?? [];
  const canDryRun = job?.kind === "data" && dryKey.length > 0;
  const onDryRun = async () => {
    if (!job) return;
    setDryOpen(true); setDryLoading(true); setDryErr(null); setDry(null);
    try {
      const r = await api.dryRun({
        source: job.source as unknown as Record<string, unknown>,
        target: job.target as unknown as Record<string, unknown>,
        key: dryKey,
        columns: (opts.columns as string[]) ?? undefined,
        column_mappings: (opts.column_mappings as Record<string, string>) ?? undefined,
        limit: 1000,
      });
      setDry(r.data);
    } catch (e) {
      setDryErr((e as Error).message);
    } finally {
      setDryLoading(false);
    }
  };

  const onRun = async () => {
    setRunning(true);
    setErr(null);
    try {
      await api.runCompare(jobId, reqVars.length ? varVals : undefined);
      load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setRunning(false);
    }
  };

  const onDelete = async () => {
    if (!confirm("Delete this test case? Its result and stored samples will be removed.")) return;
    setBusy(true);
    try {
      await api.deleteCompare(jobId);
      navigate("/validation");
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };
  useEffect(() => {
    if (job?.kind === "schema")
      api.compareDifferences(jobId).then((r) => setSchemaRecs(r.data as SchemaRec[]));
  }, [job?.kind, jobId]);

  if (err) return <p className="text-sm text-rose-600">{err}</p>;
  if (!job) return <Spinner />;

  const s = (job.result?.summary ?? {}) as Record<string, number>;
  const match = (s as { match?: boolean }).match;
  const stoppedEarly = Boolean((s as Record<string, unknown>).stopped_early);

  return (
    <div className="space-y-6">
      <div>
        <Link
          to={job.project_id ? `/validation/projects/${job.project_id}?tab=runs` : "/validation"}
          className="text-sm text-slate-400 hover:text-slate-600"
        >
          ← {job.project_id ? "Back to executions" : "Data Validation"}
        </Link>
        <div className="mt-1 flex items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold text-navy">{job.name || job.id.slice(0, 8)}</h1>
            <span className="rounded bg-slate-100 px-2 py-0.5 text-xs">{job.kind}</span>
            {job.status === "failed" ? (
              <HealthBadge status="unknown" label="Did not run" />
            ) : match === true ? (
              <HealthBadge status="healthy" label="Match" />
            ) : match === false ? (
              <HealthBadge status="critical" label="Differences" />
            ) : (
              <HealthBadge status="unknown" label={job.status} />
            )}
            <FailureTrendBadge trend={trend} />
          </div>
          <div className="flex items-center gap-2">
            {job.result && (
              <>
                <a href={api.compareExportUrl(jobId, "pdf")}>
                  <Button variant="ghost">Export PDF</Button>
                </a>
                <a href={api.compareExportUrl(jobId, "csv")}>
                  <Button variant="ghost">CSV</Button>
                </a>
              </>
            )}
            <Link to={`/validation/${jobId}/edit`}>
              <Button variant="ghost">Edit</Button>
            </Link>
            <Link to={`/validation/${jobId}/clone`}>
              <Button variant="ghost">Duplicate</Button>
            </Link>
            <Button variant="ghost" onClick={onDelete} disabled={busy}>
              {busy ? "Deleting…" : "Delete"}
            </Button>
          </div>
        </div>
        <div className="mt-2"><KeyDetectionNote job={job} /></div>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-400">
          <span>
            {job.source.table ?? "(sql)"} → {job.target.table ?? "(sql)"}
            {job.result?.detected_key?.length ? ` · key: ${job.result.detected_key.join(", ")}` : ""}
          </span>
          <span className="flex items-center gap-1">
            · Project:
            <select
              value={job.project_id ?? ""}
              onChange={(e) => onAssignProject(e.target.value)}
              className="rounded border border-slate-200 bg-white px-1.5 py-0.5 text-xs text-slate-600"
            >
              <option value="">None</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </span>
          {job.schedule_cron && (
            <span className="flex items-center gap-1">
              · <ScheduledIcon cron={job.schedule_cron} nextRun={job.next_run ?? null} />
            </span>
          )}
        </div>
        {job.result?.variables && Object.keys(job.result.variables).length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <span className="text-xs text-slate-400">Variables used:</span>
            {Object.entries(job.result.variables).map(([k, v]) => (
              <span key={k} className="rounded bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-600">
                {k}={String(v)}
              </span>
            ))}
          </div>
        )}
      </div>

      <Card className="space-y-3 p-4">
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold text-navy">
            {reqVars.length ? "Run with variables" : "Run test case"}
          </div>
          <div className="flex items-center gap-2">
            {canDryRun && (
              <span title="Run the real compare on ~1,000 sample rows without saving — catch a wrong key/mapping before the full run">
                <Button variant="ghost" onClick={onDryRun} disabled={dryLoading || running}>
                  {dryLoading ? "Testing…" : "🧪 Dry-run on sample"}
                </Button>
              </span>
            )}
            <Button onClick={onRun} disabled={running || !varsReady}>
              {running ? "Running…" : job.result ? "Re-run" : "Run"}
            </Button>
          </div>
        </div>
        {reqVars.length > 0 ? (
          <>
            <p className="text-xs text-slate-500">
              This test case references {reqVars.length} variable{reqVars.length === 1 ? "" : "s"} in its
              table / SQL / schema / database. Supply values to execute — they’re recorded with the run.
            </p>
            <div className="grid grid-cols-2 gap-3">
              {reqVars.map((v) => (
                <Field key={v} label={`{{${v}}}`}>
                  <TextInput value={varVals[v] ?? ""} placeholder={`value for ${v}`}
                    onChange={(e) => setVarVals((p) => ({ ...p, [v]: e.target.value }))} />
                </Field>
              ))}
            </div>
          </>
        ) : (
          <p className="text-xs text-slate-500">No variables — runs the test exactly as defined.</p>
        )}
      </Card>

      {job.error && (
        <div className="rounded-lg border border-rose-300 bg-rose-50 p-3 text-sm">
          <div className="font-semibold text-rose-800">{job.status === "failed" ? "This test did not run" : "Error"}</div>
          <p className="mt-1 break-words font-mono text-xs text-rose-700">{job.error}</p>
          {job.error_hint && <p className="mt-2 text-xs text-rose-700">💡 {job.error_hint}</p>}
        </div>
      )}
      {/* Non-fatal warnings (e.g. the sample guard couldn't verify, hash unavailable) — surfaced
          here so the user is told when something couldn't be determined, not just in the log. */}
      {(() => {
        const warns = (job.result?.warnings ?? []).filter((w) => w.code !== "hash_precheck_passed" && w.message);
        return warns.length > 0 ? (
          <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm">
            <div className="font-semibold text-amber-800">⚠ Heads up</div>
            <ul className="mt-1 list-disc space-y-1 pl-5 text-amber-700">
              {warns.map((w, i) => <li key={i}>{w.message}</li>)}
            </ul>
          </div>
        ) : null;
      })()}
      <Modal open={dryOpen} onClose={() => setDryOpen(false)} wide title="Dry-run on sample">
        <DryRunView loading={dryLoading} err={dryErr} data={dry} />
      </Modal>

      {/* Execution log — expanded by default for failed/cancelled runs so you can see what happened. */}
      <ExecutionLog jobId={jobId} defaultOpen={job.status === "failed" || job.status === "cancelled"} />
      {job.result?.sample_location && (
        <p className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600">
          Exception rows are in your warehouse:{" "}
          <span className="font-mono text-navy">{job.result.sample_location}</span>
        </p>
      )}
      {job.result && stoppedEarly && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          <span className="font-semibold">⚠ Fail-fast stopped this run early.</span>{" "}
          Examined {(s.keys_total ?? 0).toLocaleString()} of {(s.src_row_count ?? 0).toLocaleString()} source rows and
          confirmed {(s.total_diff_rows ?? 0).toLocaleString()} differences before stopping. These counts are a
          lower bound, not the full set — disable fail-fast (or widen the budget) and re-run for a complete compare.
        </div>
      )}
      {!job.result ? (
        <p className="text-sm text-slate-400">Not run yet.</p>
      ) : job.kind === "count" ? (
        <CountView s={s} job={job} />
      ) : job.kind === "schema" ? (
        <SchemaView s={s} recs={schemaRecs} />
      ) : (
        <DataView s={s} jobId={jobId} />
      )}
    </div>
  );
}
