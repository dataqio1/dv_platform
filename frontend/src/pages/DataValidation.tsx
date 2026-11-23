import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { usePrefs } from "../prefs";
import { useProjects } from "../activeProject";
import type { CompareBatch, CompareJob, Connection, RunEntry, Tag } from "../types";
import { Button, Card, EmptyState, HealthBadge, Modal, Spinner } from "../components/ui";
import { ExecutionLog } from "../components/ExecutionLog";
import { ProjectSwitcher } from "../components/ProjectSwitcher";
import { TrendDots, type RunOutcome } from "../components/viz";
import { ScheduledIcon } from "../components/ScheduleControl";
import { TestCaseWizard, hasWizardDraft, type CompareSeed } from "../components/TestCaseWizard";
import { ProjectDetail } from "./Projects";
import { fmtDateTime } from "../tz";

function matchBadge(job: CompareJob) {
  if (job.status === "queued") return <RunningBadge label="Queued" />;
  if (job.status === "running") return <RunningBadge label="Running" />;
  if (job.status === "cancelled") return <HealthBadge status="warning" label="Cancelled" title="The run was cancelled before it finished." />;
  if (job.status === "failed") return <HealthBadge status="unknown" label="Did not run" title={job.error ? `Did not run: ${job.error}${job.error_hint ? `\n\n💡 ${job.error_hint}` : ""}` : "The compare errored before producing a result (e.g. connection, query, or permission issue) — open it to see the error. This is different from 'Differences'."} />;
  const m = (job.summary as { match?: boolean } | null | undefined)?.match;
  if (m === true) return <HealthBadge status="healthy" label="Match" title="Source and target are identical within the test's tolerance." />;
  if (m === false) return <HealthBadge status="critical" label="Differences" title="The compare ran successfully and found differences — click to see which rows/columns differ." />;
  return <HealthBadge status="unknown" label={job.status} />;
}

function RunningBadge({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-sky-50 px-2 py-0.5 text-xs font-medium text-sky-700 ring-1 ring-inset ring-sky-200">
      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-sky-500" />
      {label}
    </span>
  );
}

function batchBadge(b: CompareBatch) {
  if (b.status === "running") return <RunningBadge label={b.running ? `Running ${b.total - b.running}/${b.total}` : "Running"} />;
  if (b.status === "cancelled") return <HealthBadge status="warning" label="Cancelled" />;
  if (b.status === "succeeded") return <HealthBadge status="healthy" label="All match" title="Every table in this test case matched." />;
  if (b.status === "differences")
    return <HealthBadge status="critical" label={b.total > 1 ? `Differences (${b.differed}/${b.total})` : "Differences"} title="One or more tables found differences — click to see which." />;
  if (b.status === "partial") return <HealthBadge status="warning" label="Partial" title="Some tables ran, others errored or were cancelled — results are incomplete." />;
  return <HealthBadge status="critical" label="Failed" title="The test case errored before producing results — open it to see the error." />;
}

const KIND_LABEL: Record<string, string> = {
  count: "Counts compare",
  schema: "Schema compare",
  data: "Data validation",
};

// ── health classification: a test case's LATEST result, and a run's outcome ──
type CaseState = RunOutcome | "running" | "notrun";
function singleResult(j: CompareJob): CaseState {
  if (j.status === "running" || j.status === "queued") return "running";
  if (j.status === "failed" || j.status === "cancelled") return "fail";
  const m = (j.summary as { match?: boolean } | null | undefined)?.match;
  return m === true ? "pass" : m === false ? "diff" : "notrun";
}
function batchResult(b: CompareBatch): CaseState {
  if (b.status === "running") return "running";
  if (b.status === "succeeded") return "pass";
  if (b.status === "differences") return "diff";
  if (b.status === "failed" || b.status === "partial" || b.status === "cancelled") return "fail";
  return "notrun";
}
function runOutcome(e: RunEntry): RunOutcome {
  if (e.type === "single") {
    const r = e.run;
    if (!r || r.status === "failed" || r.status === "cancelled") return "fail";
    return r.match === true ? "pass" : r.match === false ? "diff" : "fail";
  }
  return e.status === "succeeded" ? "pass" : e.status === "differences" ? "diff" : "fail";
}
// Status accent + dot colour for a test-case row.
const STATE_ACCENT: Record<CaseState, string> = {
  pass: "border-l-emerald-400", diff: "border-l-rose-400", fail: "border-l-amber-400",
  running: "border-l-sky-400", notrun: "border-l-slate-200",
};
const STATE_DOT: Record<CaseState, string> = {
  pass: "bg-emerald-500", diff: "bg-rose-500", fail: "bg-amber-500",
  running: "bg-sky-500 animate-pulse", notrun: "bg-slate-300",
};

/** "3m ago" / "2h ago" / "5d ago" — compact relative time. */
function ago(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso).getTime();
  if (isNaN(d)) return "—";
  const s = Math.max(0, (Date.now() - d) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
  return fmtDateTime(iso, { hour: undefined, minute: undefined });
}
function duration(a?: string | null, b?: string | null): string {
  if (!a || !b) return "";
  const ms = new Date(b).getTime() - new Date(a).getTime();
  if (isNaN(ms) || ms < 0) return "";
  const s = ms / 1000;
  return s < 1 ? "<1s" : s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

const PAGE_SIZE = 25;

/** Client-side search + pagination over an already-loaded list. Fine for the hundreds of
 *  test cases / executions a project accumulates; the full set is fetched once and we filter
 *  and page in-memory. Resets to page 1 whenever the query changes or the list shrinks. */
function useSearchPage<T>(items: T[], matches: (item: T, q: string) => boolean) {
  const [q, setQ] = useState("");
  const [page, setPage] = useState(0);
  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return needle ? items.filter((it) => matches(it, needle)) : items;
  }, [items, q]); // eslint-disable-line react-hooks/exhaustive-deps
  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const clamped = Math.min(page, pages - 1);
  const slice = filtered.slice(clamped * PAGE_SIZE, clamped * PAGE_SIZE + PAGE_SIZE);
  useEffect(() => { setPage(0); }, [q]);
  return { q, setQ, slice, page: clamped, pages, setPage, shown: filtered.length, total: items.length };
}

const PG_BTN = "rounded-lg border border-slate-300 px-2 py-1 text-xs font-medium text-slate-600 enabled:hover:border-teal enabled:hover:text-teal-700 disabled:opacity-40";

/** Prev / page-of / next controls + a "N shown" count. Rendered only when there's >1 page. */
function Pager({ label, page, pages, setPage, shown, total }: {
  label?: string; page: number; pages: number; setPage: (p: number) => void; shown: number; total: number;
}) {
  return (
    <div className="flex items-center gap-2 text-xs text-slate-500">
      <span>{label ? `${label}: ` : ""}{shown === total ? `${total}` : `${shown} of ${total}`}</span>
      {pages > 1 && (
        <>
          <button className={PG_BTN} disabled={page <= 0} onClick={() => setPage(page - 1)}>‹ Prev</button>
          <span>Page {page + 1} / {pages}</span>
          <button className={PG_BTN} disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>Next ›</button>
        </>
      )}
    </div>
  );
}

/** Search box (with clear button) — pair it with one or more <Pager/>s. */
function SearchBox({ q, setQ, placeholder }: { q: string; setQ: (v: string) => void; placeholder: string }) {
  return (
    <div className="relative">
      <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400">⌕</span>
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder={placeholder}
        className="w-72 rounded-lg border border-slate-300 py-1.5 pl-7 pr-7 text-sm outline-none focus:border-teal"
      />
      {q && (
        <button onClick={() => setQ("")} className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600">×</button>
      )}
    </div>
  );
}

/** Lowercased haystack for a job/batch row — name, kind, source/target tables, actors. */
function jobHaystack(j: CompareJob): string {
  return [j.name, j.id, j.kind, j.source?.table, j.target?.table, j.created_by_name, j.last_run_by_name]
    .filter(Boolean).join(" ").toLowerCase();
}
function batchHaystack(b: CompareBatch): string {
  return [b.name, b.batch_id, b.kind, b.created_by_name, b.last_run_by_name]
    .filter(Boolean).join(" ").toLowerCase();
}

/** Opened from an incident. Target table, connection, and key are already filled. */
export function CompareFromIncident() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  const [connections, setConnections] = useState<Connection[] | null>(null);
  useEffect(() => {
    api.listConnections().then((r) => setConnections(r.data)).catch(() => setConnections([]));
  }, []);
  if (connections === null) return <Spinner />;
  const seed: CompareSeed = {
    incidentId: params.get("incident") ?? "",
    connectionId: params.get("connection") ?? "",
    database: params.get("database") ?? "",
    schema: params.get("schema") ?? "",
    table: params.get("table") ?? "",
    key: params.get("key") ?? "",
  };
  return (
    <div className="space-y-4">
      <button type="button" onClick={() => nav(-1)} className="text-sm text-slate-400 hover:text-slate-600">
        ← Back
      </button>
      <TestCaseWizard connections={connections} seed={seed} onCancel={() => nav(-1)} onNavigate={(p) => nav(p)} />
    </div>
  );
}

// A single project's workspace: test cases + execution history.
export default function DataValidation() {
  const nav = useNavigate();
  const { projectId = "" } = useParams();
  const { setPref, can, recordRecent, loaded: prefsLoaded, prefs } = usePrefs();
  const { projects, loading: projectsLoading, refresh: refreshProjects, setActive } = useProjects();
  const canManage = can("manage_projects");
  const project = projects.find((p) => p.id === projectId) ?? null;
  const [jobs, setJobs] = useState<CompareJob[] | null>(null);
  const [batches, setBatches] = useState<CompareBatch[]>([]);
  const [runs, setRuns] = useState<RunEntry[]>([]);  // execution history (every run)
  const [connections, setConnections] = useState<Connection[]>([]);
  const [allTags, setAllTags] = useState<Tag[]>([]);
  const [adding, setAdding] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();
  // Tab lives in the URL (?tab=cases|runs) so a refresh or shared link lands on the same tab.
  const tab: "cases" | "runs" = searchParams.get("tab") === "runs" ? "runs" : "cases";
  const setTab = (k: "cases" | "runs") => {
    const next = new URLSearchParams(searchParams);
    next.set("tab", k);
    setSearchParams(next, { replace: true });
  };
  const [rerunning, setRerunning] = useState<Set<string>>(new Set());

  const load = () => {
    api.listCompares().then((r) => setJobs(r.data)).catch(() => setJobs([]));
    api.listBatches().then((r) => setBatches(r.data)).catch(() => setBatches([]));
    api.listRuns(projectId).then((r) => setRuns(r.data)).catch(() => setRuns([]));
  };
  useEffect(() => {
    load();
    api.listConnections().then((r) => setConnections(r.data));
    api.listTags().then((r) => setAllTags(r.data)).catch(() => setAllTags([]));
    if (hasWizardDraft()) setAdding(true);
    setPref("lastProjectId", projectId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);
  // Gate on prefsLoaded so we don't set the active project before server prefs load (which
  // would otherwise clobber our choice) — keeps the top-bar project in sync with the URL.
  useEffect(() => {
    if (!prefsLoaded) return;
    if (projectId) {
      setActive(projectId);
      // Stamp this project's per-user "last viewed" time (drives the Projects list sort). Gated on
      // prefsLoaded so we merge into the loaded map rather than clobbering it with an empty one.
      const seen = (prefs.projectLastViewed as Record<string, string> | undefined) ?? {};
      setPref("projectLastViewed", { ...seen, [projectId]: new Date().toISOString() });
    }
    if (project) recordRecent({ kind: "Project", id: project.id, name: project.name, path: `/validation/projects/${project.id}` });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project?.id, project?.name, prefsLoaded, projectId]);

  const connName = (id?: string) => connections.find((c) => c.id === id)?.name ?? id?.slice(0, 8) ?? "?";
  const projectName = project?.name ?? "";

  const singles = (jobs ?? []).filter((j) => !j.batch_id && j.project_id === projectId);
  const scopedBatches = batches.filter((b) => b.project_id === projectId);
  const nothing = singles.length === 0 && scopedBatches.length === 0;

  // Project health (from the LATEST result of each test case) + a 7-day pass-rate trend (from the
  // run history) — the inputs for the dashboard header.
  const health = useMemo(() => {
    const states: CaseState[] = [...singles.map(singleResult), ...scopedBatches.map(batchResult)];
    const n = (st: CaseState) => states.filter((x) => x === st).length;
    const passing = n("pass"), differences = n("diff"), failed = n("fail"), running = n("running"), notRun = n("notrun");
    const done = passing + differences + failed;
    const DAY = 86_400_000, now = Date.now();
    const buckets = Array.from({ length: 7 }, () => ({ pass: 0, total: 0 }));
    const dayLabel = (i: number) => new Date(now - (6 - i) * DAY).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    for (const e of runs) {
      const t = e.when ? new Date(e.when).getTime() : NaN;
      if (isNaN(t)) continue;
      const idx = 6 - Math.floor((now - t) / DAY);
      if (idx < 0 || idx > 6) continue;
      buckets[idx].total++;
      if (runOutcome(e) === "pass") buckets[idx].pass++;
    }
    return {
      passing, differences, failed, running, notRun,
      passRate: done > 0 ? (passing / done) * 100 : 0,
      trend: buckets.map((b) => (b.total ? b.pass / b.total : null)),
      trendLabels: buckets.map((b, i) => `${dayLabel(i)}: ${b.total ? `${Math.round((100 * b.pass) / b.total)}% of ${b.total} run(s)` : "no runs"}`),
    };
  }, [singles, scopedBatches, runs]);

  // Last-8 run outcomes per test case (oldest → newest) for the per-row mini trend.
  const trendByCase = useMemo(() => {
    const map = new Map<string, RunOutcome[]>();
    for (const e of runs) {
      const id = e.type === "single" ? (e.run?.job_id ?? e.id) : (e.batch_id ?? e.id);
      const arr = map.get(id) ?? [];
      if (arr.length < 8) arr.push(runOutcome(e));
      map.set(id, arr);
    }
    for (const [k, v] of map) map.set(k, v.reverse());
    return map;
  }, [runs]);

  // While anything is executing, poll so statuses advance (queued → running → done) live —
  // the user navigated here from "Run" and shouldn't have to refresh.
  const anyRunning =
    singles.some((j) => j.status === "running" || j.status === "queued") ||
    scopedBatches.some((b) => b.status === "running");
  const loadRef = useRef(load);
  loadRef.current = load;
  useEffect(() => {
    if (!anyRunning) return;
    const id = setInterval(() => loadRef.current(), 3000);
    return () => clearInterval(id);
  }, [anyRunning]);

  // Rerun a single test case OR a whole batch — both async (non-blocking); the list then
  // polls the run to completion. `id` is a job id unless `kind: "batch"`.
  const rerun = async (id: string, kind: "single" | "batch" = "single") => {
    setRerunning((s) => new Set(s).add(id));
    try {
      await (kind === "batch" ? api.rerunBatch(id) : api.submitCompare(id));
      load();
    } catch {
      /* surfaced on the detail page */
    } finally {
      // Clear after a moment so the row shows progress via polling, not a stuck spinner.
      setTimeout(() => setRerunning((s) => { const n = new Set(s); n.delete(id); return n; }), 1500);
    }
  };

  // Cancel a running/queued execution — stops the engine and aborts the in-flight query.
  const cancel = async (id: string, kind: "single" | "batch" = "single") => {
    setRerunning((s) => new Set(s).add(id));
    try {
      await (kind === "batch" ? api.cancelBatch(id) : api.cancelCompare(id));
      load();
    } catch {
      /* ignore */
    } finally {
      setTimeout(() => setRerunning((s) => { const n = new Set(s); n.delete(id); return n; }), 800);
    }
  };

  // Project not found (archived / bad link) once the list has loaded.
  if (!projectsLoading && !project) {
    return (
      <div className="space-y-4">
        <Link to="/validation" className="text-sm text-slate-400 hover:text-slate-600">← All projects</Link>
        <EmptyState title="Project not found" hint="It may have been archived. Go back to the project list." />
      </div>
    );
  }

  // ── Focused creation flow: only the wizard, nothing else. ──
  if (adding) {
    return (
      <div className="mx-auto max-w-7xl space-y-4">
        <div className="flex items-center gap-2 text-sm text-slate-400">
          <Link to="/validation" className="hover:text-slate-600">Data Validation</Link>
          <span>/</span>
          <button onClick={() => setAdding(false)} className="hover:text-slate-600">{projectName || "Project"}</button>
          <span>/</span>
          <span className="font-medium text-navy">New test case</span>
        </div>
        <TestCaseWizard
          connections={connections}
          projectId={projectId}
          projectName={projectName}
          onCancel={() => setAdding(false)}
          onNavigate={(p) => { setAdding(false); load(); refreshProjects(); nav(p); }}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Breadcrumb + project switcher — jump between projects without going back. */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm">
          <Link to="/validation" className="text-slate-400 hover:text-slate-600">Data Validation</Link>
          <span className="text-slate-300">/</span>
          <ProjectSwitcher />
        </div>
        <div className="flex items-center gap-2">
          <Button onClick={() => setAdding(true)} disabled={connections.length === 0}>+ New test case</Button>
        </div>
      </div>

      {/* Compact project header (stats, edit/archive/report) */}
      {project && (
        <ProjectDetail project={project} allTags={allTags} canManage={canManage} hideTestCases
          runCount={runs.length} health={health}
          onChanged={refreshProjects} onDeleted={() => nav("/validation")} />
      )}

      {connections.length === 0 && (
        <EmptyState title="Add a connection first" hint="Go to Administration → Connections to add a data source, then create a test case." />
      )}

      {/* Tabs */}
      {connections.length > 0 && (
        <div className="flex items-center gap-6 border-b border-slate-200">
          {([
            ["cases", "Test cases", singles.length + scopedBatches.length, "The reusable test definitions in this project. Click a name to edit it; click ▶ to run."],
            ["runs", "Executions", runs.length, "The history of every run — newest first — with its match/difference result. Click a row to see the differences."],
          ] as const).map(([k, label, n, tip]) => (
            <button key={k} onClick={() => setTab(k)} title={tip}
              className={`-mb-px border-b-2 px-0.5 pb-2.5 text-sm font-medium transition ${
                tab === k ? "border-teal text-navy" : "border-transparent text-slate-500 hover:text-slate-700"}`}>
              {label} <span className="ml-1.5 text-xs text-slate-400">· {n}</span>
            </button>
          ))}
        </div>
      )}

      {jobs === null || projectsLoading ? (
        <Spinner />
      ) : connections.length === 0 ? null : nothing ? (
        <EmptyState
          title={projectName ? `No test cases in ${projectName} yet` : "No test cases yet"}
          hint="Create one to compare datasets or check data quality."
        />
      ) : tab === "cases" ? (
        <TestCasesTab
          singles={singles} batches={scopedBatches} connName={connName}
          rerun={rerun} cancel={cancel} rerunning={rerunning} trendByCase={trendByCase}
        />
      ) : (
        <ExecutionsTab runs={runs} rerun={rerun} cancel={cancel} rerunning={rerunning} />
      )}
    </div>
  );
}

// ── Test cases: definitions (singles + batches), each with Run + open ──
function TestCasesTab({ singles, batches, connName, rerun, cancel, rerunning, trendByCase }: {
  singles: CompareJob[];
  batches: CompareBatch[];
  connName: (id?: string) => string;
  rerun: (id: string, kind?: "single" | "batch") => void;
  cancel: (id: string, kind?: "single" | "batch") => void;
  rerunning: Set<string>;
  trendByCase: Map<string, RunOutcome[]>;
}) {
  // One search box drives both lists; each list keeps its own page so a long list of singles
  // doesn't push the (usually few) batches off-screen.
  const [q, setQ] = useState("");
  const needle = q.trim().toLowerCase();
  const fBatches = needle ? batches.filter((b) => batchHaystack(b).includes(needle)) : batches;
  const fSingles = needle ? singles.filter((j) => jobHaystack(j).includes(needle)) : singles;
  const [bPage, setBPage] = useState(0);
  const [sPage, setSPage] = useState(0);
  useEffect(() => { setBPage(0); setSPage(0); }, [q]);
  const bPages = Math.max(1, Math.ceil(fBatches.length / PAGE_SIZE));
  const sPages = Math.max(1, Math.ceil(fSingles.length / PAGE_SIZE));
  const bClamp = Math.min(bPage, bPages - 1);
  const sClamp = Math.min(sPage, sPages - 1);
  const batchSlice = fBatches.slice(bClamp * PAGE_SIZE, bClamp * PAGE_SIZE + PAGE_SIZE);
  const singleSlice = fSingles.slice(sClamp * PAGE_SIZE, sClamp * PAGE_SIZE + PAGE_SIZE);
  const nothingMatches = needle && fBatches.length === 0 && fSingles.length === 0;
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <SearchBox q={q} setQ={setQ} placeholder="Search test cases by name, table, author…" />
        <span className="text-xs text-slate-500">
          {needle ? `${fBatches.length + fSingles.length} of ${batches.length + singles.length}` : `${batches.length + singles.length}`} test cases
        </span>
      </div>
      {nothingMatches && <EmptyState title="No matches" hint={`Nothing matches “${q}”.`} />}
      {fBatches.length > 0 && (
        <Card>
          <div className="flex items-center justify-between border-b border-slate-100 px-5 py-3">
            <span className="text-sm font-semibold text-navy">Multi-dataset compares</span>
            <Pager label="batches" page={bClamp} pages={bPages} setPage={setBPage} shown={fBatches.length} total={batches.length} />
          </div>
          <div>
            {batchSlice.map((b) => {
              const st = batchResult(b);
              // Click a test case's name to edit it in the wizard: a 1-pair batch edits that single
              // pair; a multi-table batch opens the batch editor (step through / add / remove pairs).
              const onlyJobId = b.total === 1 ? b.jobs?.[0]?.id : undefined;
              const nameHref = onlyJobId ? `/validation/${onlyJobId}/edit` : `/validation/batch/${b.batch_id}/edit`;
              return (
                <div key={b.batch_id} className={`flex items-center gap-3 border-b border-l-[3px] border-slate-50 ${STATE_ACCENT[st]} px-4 py-3 last:border-b-0 hover:bg-slate-50/60`}>
                  <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${STATE_DOT[st]}`} />
                  <div className="min-w-0 flex-1">
                    <Link to={nameHref} className="font-semibold text-navy hover:text-teal-700 hover:underline">{b.name}</Link>
                    <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">{KIND_LABEL[b.kind] ?? b.kind}</span>
                    <span className="ml-1.5 text-[10px] text-slate-400">{b.total} pairs</span>
                    <div className="truncate text-[11px] text-slate-400">{b.matched}/{b.total} match · {b.failed > 0 ? `${b.failed} failed · ` : ""}by {b.created_by_name ?? "—"}</div>
                  </div>
                  <div className="hidden items-end sm:flex"><TrendDots results={trendByCase.get(b.batch_id) ?? []} /></div>
                  <div className="hidden w-20 text-right text-[11px] text-slate-400 md:block">{ago(b.finished_at)}</div>
                  <div className="w-28 text-right">{batchBadge(b)}</div>
                  <ScheduledIcon cron={b.schedule_cron ?? null} nextRun={b.next_run ?? null} />
                  <RerunButton id={b.batch_id} kind="batch" rerun={rerun} cancel={cancel} busy={rerunning.has(b.batch_id)} running={b.status === "running"} />
                  <Link to={`/validation/batch/${b.batch_id}`} className="text-xs font-medium text-slate-400 hover:text-navy">Open →</Link>
                </div>
              );
            })}
          </div>
        </Card>
      )}

      {fSingles.length > 0 && (
        <Card>
          <div className="flex items-center justify-between border-b border-slate-100 px-5 py-3">
            <span className="text-sm font-semibold text-navy">Single-dataset test cases</span>
            <Pager label="test cases" page={sClamp} pages={sPages} setPage={setSPage} shown={fSingles.length} total={singles.length} />
          </div>
          <div>
            {singleSlice.map((j) => {
              const st = singleResult(j);
              return (
                <div key={j.id} className={`flex items-center gap-3 border-b border-l-[3px] border-slate-50 ${STATE_ACCENT[st]} px-4 py-3 last:border-b-0 hover:bg-slate-50/60`}>
                  <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${STATE_DOT[st]}`} />
                  <div className="min-w-0 flex-1">
                    <Link to={`/validation/${j.id}/edit`} className="font-semibold text-navy hover:text-teal-700 hover:underline">{j.name || j.id.slice(0, 8)}</Link>
                    <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">{KIND_LABEL[j.kind] ?? j.kind}</span>
                    <div className="truncate text-[11px] text-slate-400">
                      {connName(j.source.connection_id)}.{j.source.table ?? "(sql)"} → {connName(j.target.connection_id)}.{j.target.table ?? "(sql)"} · by {j.created_by_name ?? "—"}
                    </div>
                  </div>
                  <div className="hidden items-end sm:flex"><TrendDots results={trendByCase.get(j.id) ?? []} /></div>
                  <div className="hidden w-20 text-right text-[11px] text-slate-400 md:block">{ago(j.finished_at)}</div>
                  <div className="w-28 text-right">{j.status === "running" && j.progress ? <ProgressChip p={j.progress} /> : matchBadge(j)}</div>
                  <ScheduledIcon cron={j.schedule_cron ?? null} nextRun={j.next_run ?? null} />
                  <RerunButton id={j.id} rerun={rerun} cancel={cancel} busy={rerunning.has(j.id)} running={j.status === "running" || j.status === "queued"} />
                </div>
              );
            })}
          </div>
        </Card>
      )}
    </div>
  );
}

function runHaystack(e: RunEntry): string {
  const r = e.run;
  return [e.name, e.id, e.kind, e.last_run_by_name, r?.name, r?.job_id, r?.kind]
    .filter(Boolean).join(" ").toLowerCase();
}

/** Result badge for one history entry (single run, or an aggregated batch run). */
function runBadge(e: RunEntry) {
  if (e.type === "single") {
    const r = e.run!;
    if (r.status === "cancelled") return <HealthBadge status="warning" label="Cancelled" />;
    if (r.status === "failed") return <HealthBadge status="unknown" label="Did not run" />;
    if (r.match === true) return <HealthBadge status="healthy" label="Match" />;
    if (r.match === false) return <HealthBadge status="critical" label="Differences" />;
    return <HealthBadge status="unknown" label={r.status} />;
  }
  if (e.status === "cancelled") return <HealthBadge status="warning" label="Cancelled" />;
  if (e.status === "succeeded") return <HealthBadge status="healthy" label="All match" />;
  if (e.status === "differences")
    return <HealthBadge status="critical" label={(e.total ?? 0) > 1 ? `Differences (${e.differed}/${e.total})` : "Differences"} />;
  if (e.status === "partial") return <HealthBadge status="warning" label="Partial" />;
  return <HealthBadge status="critical" label="Failed" />;
}

/** Span of a batch run: earliest start → latest finish across its pair-runs. */
function batchDuration(e: RunEntry): string {
  const starts = (e.runs ?? []).map((r) => r.started_at).filter(Boolean).sort() as string[];
  const ends = (e.runs ?? []).map((r) => r.finished_at).filter(Boolean).sort() as string[];
  return starts.length && ends.length ? duration(starts[0], ends[ends.length - 1]) : "";
}

/** Pop-up that shows ONLY the execution log(s) for a run — no results page. A batch run shows
 *  one log section per pair. Logs are read from the run snapshot, so each past run keeps its own. */
function LogModal({ entry, onClose }: { entry: RunEntry | null; onClose: () => void }) {
  const title = entry ? (entry.type === "single" ? (entry.run?.name || entry.id.slice(0, 8)) : (entry.name || entry.id.slice(0, 8))) : "";
  const runs = entry?.type === "batch" ? (entry.runs ?? []) : [];
  return (
    <Modal open={!!entry} onClose={onClose} wide title={<>Execution log — <span className="font-normal text-slate-500">{title}</span></>}>
      {!entry ? null : entry.type === "single" ? (
        <ExecutionLog runId={entry.run!.id} defaultOpen />
      ) : runs.length === 0 ? (
        <p className="text-sm text-slate-400">No pairs in this run.</p>
      ) : (
        <div className="space-y-4">
          {runs.map((r) => (
            <div key={r.id}>
              <div className="mb-1 text-xs font-semibold text-navy">{r.name}</div>
              <ExecutionLog runId={r.id} defaultOpen />
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}

// ── Executions: full run history (every run is its own row; batch pairs grouped) ──
function ExecutionsTab({ runs, rerun, cancel, rerunning }: {
  runs: RunEntry[];
  rerun: (id: string, kind?: "single" | "batch") => void;
  cancel: (id: string, kind?: "single" | "batch") => void;
  rerunning: Set<string>;
}) {
  const ex = useSearchPage(runs, (e, q) => runHaystack(e).includes(q));
  const [logFor, setLogFor] = useState<RunEntry | null>(null);
  if (runs.length === 0) return <EmptyState title="No executions yet" hint="Run a test case — each run is recorded here." />;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <SearchBox q={ex.q} setQ={ex.setQ} placeholder="Search executions by name, author…" />
        <Pager label="runs" page={ex.page} pages={ex.pages} setPage={ex.setPage} shown={ex.shown} total={ex.total} />
      </div>
      {ex.slice.length === 0 ? (
        <EmptyState title="No matches" hint={`Nothing matches “${ex.q}”.`} />
      ) : (
      <Card>
      <table className="w-full text-sm">
        <thead className="border-b border-slate-200 bg-slate-50/60 text-left text-[11px] uppercase tracking-wide text-slate-400">
          <tr>
            <th className="px-5 py-3 font-medium">Test case</th>
            <th className="px-5 py-3 font-medium">When</th>
            <th className="px-5 py-3 font-medium">Run by</th>
            <th className="px-5 py-3 font-medium">Duration</th>
            <th className="px-5 py-3 font-medium">Result</th>
            <th className="px-5 py-3" />
          </tr>
        </thead>
        <tbody>
          {ex.slice.map((e) => {
            const single = e.type === "single";
            const r = e.run;
            const name = single ? (r?.name || r?.job_id?.slice(0, 8)) : (e.name || e.id.slice(0, 8));
            const kind = e.kind ?? r?.kind ?? "";
            const resultsLink = single ? `/validation/${r!.job_id}` : `/validation/batch/${e.batch_id}`;
            const dur = single ? duration(r?.started_at, r?.finished_at) : batchDuration(e);
            const rerunId = single ? (r!.job_id) : (e.batch_id as string);
            return (
              <tr key={e.id} className="border-b border-slate-100 last:border-0 hover:bg-slate-50/60">
                <td className="px-5 py-3">
                  <span className="mr-2 inline-block h-2 w-2 rounded-full align-middle" style={{ backgroundColor: { pass: "#10b981", diff: "#f43f5e", fail: "#f59e0b" }[runOutcome(e)] }} />
                  <Link to={resultsLink} className="font-medium text-navy hover:text-teal-700 hover:underline">{name}</Link>
                  <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-500">{KIND_LABEL[kind] ?? kind}</span>
                  {!single && <span className="ml-2 text-[11px] text-slate-400">{e.total} pairs</span>}
                </td>
                <td className="px-5 py-3 text-xs text-slate-500" title={fmtDateTime(e.when)}>{ago(e.when)}</td>
                <td className="px-5 py-3 text-xs text-slate-500">{e.last_run_by_name ?? "—"}</td>
                <td className="px-5 py-3 text-xs text-slate-400">{dur}</td>
                <td className="px-5 py-3">{runBadge(e)}</td>
                <td className="px-5 py-3">
                  <div className="flex items-center justify-end gap-3">
                    <RerunButton id={rerunId} kind={single ? "single" : "batch"} rerun={rerun} cancel={cancel} busy={rerunning.has(rerunId)} />
                    <Link to={resultsLink} className="text-xs font-medium text-teal-700 hover:text-teal-800">Results</Link>
                    <button onClick={() => setLogFor(e)} className="text-xs font-medium text-slate-500 hover:text-navy">Logs</button>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Card>
      )}
      <LogModal entry={logFor} onClose={() => setLogFor(null)} />
    </div>
  );
}

/** Live progress for a long-running compare (streaming large tables): a % + mini bar. */
function ProgressChip({ p }: { p: { pct: number; processed: number; total: number } }) {
  const pct = Math.min(100, Math.max(0, p.pct));
  return (
    <div className="inline-flex flex-col items-end gap-0.5" title={`${p.processed.toLocaleString()} / ${p.total.toLocaleString()} rows compared`}>
      <span className="text-[11px] font-semibold text-teal-700">{pct}%</span>
      <span className="block h-1 w-16 overflow-hidden rounded bg-slate-100">
        <span className="block h-full rounded bg-teal-500 transition-all" style={{ width: `${pct}%` }} />
      </span>
    </div>
  );
}

function RerunButton({ id, rerun, cancel, busy, running, kind = "single" }: {
  id: string;
  rerun: (id: string, kind?: "single" | "batch") => void;
  cancel?: (id: string, kind?: "single" | "batch") => void;
  busy: boolean;
  running?: boolean;
  kind?: "single" | "batch";
}) {
  // While the execution is in progress, offer Cancel (stops the engine + aborts the query).
  // Otherwise offer Rerun. `busy` is the brief just-clicked state.
  if (running && cancel) {
    return (
      <button
        onClick={() => cancel(id, kind)}
        disabled={busy}
        className="inline-flex items-center gap-1 rounded-lg border border-rose-300 px-2.5 py-1 text-xs font-medium text-rose-600 hover:bg-rose-50 disabled:opacity-50"
      >
        {busy ? "Cancelling…" : "✕ Cancel"}
      </button>
    );
  }
  return (
    <button
      onClick={() => rerun(id, kind)}
      disabled={busy || running}
      className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-600 hover:border-teal hover:text-teal-700 disabled:opacity-50"
    >
      {busy ? "Starting…" : "▶ Rerun"}
    </button>
  );
}
