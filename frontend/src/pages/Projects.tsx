import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { usePrefs } from "../prefs";
import { useProjects } from "../activeProject";
import type { Project, ProjectTestCase, Tag } from "../types";
import { StatRing, Sparkline, Kpi } from "../components/viz";
import { Button, Card, EmptyState, Field, Spinner, TextInput } from "../components/ui";
import { SortTh, useSort } from "../components/sortable";

// Compact "time since" for the Last viewed column ("just now", "5m ago", "3d ago", else a date).
function timeAgo(iso?: string): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (!t) return "—";
  const s = Math.floor((Date.now() - t) / 1000);
  if (s < 45) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  return new Date(t).toLocaleDateString();
}

function TagChip({ name, color }: { name: string; color?: string | null }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs"
      style={{ backgroundColor: (color ?? "#64748b") + "22", color: color ?? "#475569" }}>
      <span className="h-2 w-2 rounded-full" style={{ backgroundColor: color ?? "#64748b" }} />
      {name}
    </span>
  );
}

function TagsManager({ tags, reload }: { tags: Tag[]; reload: () => void }) {
  const [name, setName] = useState("");
  const [color, setColor] = useState("#14b8a6");

  const create = async () => {
    if (!name.trim()) return;
    await api.createTag({ name, color });
    setName("");
    reload();
  };
  const rename = async (t: Tag) => {
    const next = window.prompt("Rename tag", t.name);
    if (next && next !== t.name) {
      await api.updateTag(t.id, { name: next });
      reload();
    }
  };
  const remove = async (t: Tag) => {
    if (window.confirm(`Delete tag "${t.name}"? It will be removed from ${t.usage ?? 0} item(s).`)) {
      await api.deleteTag(t.id);
      reload();
    }
  };

  return (
    <Card className="space-y-3 p-5">
      <div className="text-sm font-semibold text-navy">Tags</div>
      <div className="flex items-end gap-2">
        <Field label="New tag">
          <TextInput value={name} onChange={(e) => setName(e.target.value)} placeholder="critical" />
        </Field>
        <input type="color" value={color} onChange={(e) => setColor(e.target.value)}
          className="h-9 w-10 rounded border border-slate-300" />
        <Button onClick={create} disabled={!name.trim()}>Add</Button>
      </div>
      {tags.length === 0 ? (
        <p className="text-xs text-slate-400">No tags yet. Tags can be attached to projects and test cases.</p>
      ) : (
        <div className="space-y-1">
          {tags.map((t) => (
            <div key={t.id} className="flex items-center justify-between rounded border border-slate-100 px-2 py-1.5">
              <TagChip name={t.name} color={t.color} />
              <div className="flex items-center gap-3 text-xs">
                <span className="text-slate-400">{t.usage ?? 0} uses</span>
                <button onClick={() => rename(t)} className="text-slate-500 hover:text-navy">Rename</button>
                <button onClick={() => remove(t)} className="text-rose-600 hover:underline">Delete</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

export type ProjectHealth = {
  passing: number; differences: number; failed: number; running: number; notRun: number;
  passRate: number; trend: (number | null)[]; trendLabels: string[];
};

export function ProjectDetail({
  project, allTags, canManage, hideTestCases, runCount, health, onChanged, onDeleted,
}: {
  project: Project;
  allTags: Tag[];
  canManage: boolean;
  hideTestCases?: boolean;
  runCount?: number; // total executions (run history) — shown instead of the redundant "Compares"
  health?: ProjectHealth; // when present, render the health dashboard header instead of stat boxes
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const navigate = useNavigate();
  const [tcs, setTcs] = useState<{ compares: ProjectTestCase[]; monitors: ProjectTestCase[] } | null>(null);
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(project.name);
  const [description, setDescription] = useState(project.description ?? "");
  const [selTags, setSelTags] = useState<string[]>(project.tags || []);
  // Project-level variables as ordered [name, value] pairs (a Record loses order while typing).
  const [vars, setVars] = useState<[string, string][]>(
    Object.entries(project.variables ?? {}).map(([k, v]) => [k, String(v)]),
  );
  const addVar = () => setVars((a) => [...a, ["", ""]]);
  const removeVar = (i: number) => setVars((a) => a.filter((_, idx) => idx !== i));
  const setVar = (i: number, j: 0 | 1, val: string) =>
    setVars((a) => a.map((p, idx) => (idx === i ? (j === 0 ? [val, p[1]] : [p[0], val]) : p)));
  const [busy, setBusy] = useState(false);
  const [reporting, setReporting] = useState(false);

  // Reporting is project-scoped: reuse a report that already targets this project, else
  // create one. Generate a fresh version and open the Reports page.
  const report = async () => {
    setReporting(true);
    try {
      const existing = (await api.listReports()).data.find(
        (r) => r.project_id === project.id && r.report_type === "data_validation"
      );
      const id = existing
        ? existing.id
        : (await api.createReport({
            name: `${project.name} — validation`,
            report_type: "data_validation",
            project_id: project.id,
            spec: {},
          })).data.id;
      await api.generateReport(id);
      navigate(`/reports/${id}`); // deep-link straight to the generated report
    } catch (e) {
      window.alert(`Could not generate report: ${(e as Error).message}`);
    } finally {
      setReporting(false);
    }
  };

  useEffect(() => {
    if (!hideTestCases) api.projectTestCases(project.id).then((r) => setTcs(r.data)).catch(() => setTcs(null));
    setEditing(false); // leaving edit mode when a different project is selected
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.id]);

  const startEdit = () => {
    setName(project.name);
    setDescription(project.description ?? "");
    setSelTags(project.tags || []);
    setVars(Object.entries(project.variables ?? {}).map(([k, v]) => [k, String(v)]));
    setEditing(true);
  };
  const save = async () => {
    if (!name.trim()) return;
    setBusy(true);
    try {
      // Drop blank-named rows; last value wins on a duplicate name.
      const variables = Object.fromEntries(
        vars.map(([k, v]) => [k.trim(), v]).filter(([k]) => k),
      );
      await api.updateProject(project.id, {
        name, description: description || null, tags: selTags, variables,
      });
      setEditing(false);
      onChanged();
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    if (!window.confirm(
      `Archive project "${project.name}"? It and its test cases will be hidden but kept, and can be restored later.`
    )) return;
    setBusy(true);
    try {
      await api.deleteProject(project.id);
      onDeleted();
    } finally {
      setBusy(false);
    }
  };

  const row = (c: ProjectTestCase, kind: string) => (
    <tr key={c.id} className="border-b border-slate-100 last:border-0">
      <td className="px-4 py-2 font-medium text-navy">{c.name || c.id.slice(0, 8)}</td>
      <td className="px-4 py-2 text-xs text-slate-500">{c.kind ?? kind}</td>
      <td className="px-4 py-2 text-xs text-slate-500">{c.source_system ?? "—"}</td>
      <td className="px-4 py-2 text-xs text-slate-500">{c.layer ?? "—"}</td>
      <td className="px-4 py-2">
        <div className="flex flex-wrap gap-1">{(c.tags || []).map((t) => <TagChip key={t} name={t} />)}</div>
      </td>
    </tr>
  );

  const all = tcs ? [...tcs.compares.map((c) => [c, "compare"] as const), ...tcs.monitors.map((m) => [m, "dq"] as const)] : [];
  const { sorted: sortedAll, sort, requestSort } = useSort(all, {
    name: ([c]) => c.name || c.id,
    kind: ([c, k]) => c.kind ?? k,
    source: ([c]) => c.source_system,
    layer: ([c]) => c.layer,
  });

  const compareCount = tcs ? tcs.compares.length : project.compare_count ?? 0;
  const monitorCount = tcs ? tcs.monitors.length : project.monitor_count ?? 0;

  return (
    <Card className="p-5">
      {editing ? (
        <div className="space-y-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label="Name">
              <TextInput value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
            <Field label="Description">
              <TextInput value={description} onChange={(e) => setDescription(e.target.value)} />
            </Field>
          </div>
          <div>
            <div className="mb-1 text-xs font-medium text-slate-400">Tags</div>
            {allTags.length === 0 ? (
              <p className="text-xs text-slate-400">No tags yet — add some in the Tags panel.</p>
            ) : (
              <div className="flex flex-wrap gap-1">
                {allTags.map((t) => {
                  const on = selTags.includes(t.name);
                  return (
                    <button key={t.id} type="button"
                      onClick={() => setSelTags(on ? selTags.filter((x) => x !== t.name) : [...selTags, t.name])}
                      className={`rounded-full px-2 py-0.5 text-xs ${on ? "bg-navy text-white" : "bg-slate-200 text-slate-600"}`}>
                      {t.name}
                    </button>
                  );
                })}
              </div>
            )}
          </div>
          <div>
            <div className="mb-1 text-xs font-medium text-slate-400">
              Variables
              <span className="ml-1 font-normal text-slate-400">
                — inherited by every test case in this project. A test case (or run) with the same
                name overrides the project value.
              </span>
            </div>
            <div className="space-y-2">
              {vars.map((pair, i) => (
                <div key={i} className="flex items-center gap-2">
                  <TextInput value={pair[0]} placeholder="name (e.g. env)"
                    onChange={(e) => setVar(i, 0, e.target.value)} />
                  <span className="shrink-0 text-slate-400">=</span>
                  <TextInput value={pair[1]} placeholder="value (e.g. PROD)"
                    onChange={(e) => setVar(i, 1, e.target.value)} />
                  <button type="button" onClick={() => removeVar(i)}
                    className="shrink-0 rounded-lg px-2 py-2 text-slate-400 hover:bg-slate-50 hover:text-rose-600"
                    aria-label="Remove variable" title="Remove">✕</button>
                </div>
              ))}
            </div>
            <Button variant="ghost" size="sm" onClick={addVar} className="mt-2">+ Add variable</Button>
          </div>
          <div className="flex gap-2">
            <Button onClick={save} disabled={busy || !name.trim()}>{busy ? "Saving…" : "Save"}</Button>
            <button onClick={() => setEditing(false)} className="text-sm text-slate-500 hover:text-navy">Cancel</button>
          </div>
        </div>
      ) : (
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="text-lg font-bold text-navy">{project.name}</div>
            {project.description && <p className="text-sm text-slate-500">{project.description}</p>}
            <div className="mt-1 flex flex-wrap gap-1">{(project.tags || []).map((t) => <TagChip key={t} name={t} />)}</div>
            {project.variables && Object.keys(project.variables).length > 0 && (
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                <span className="text-xs font-medium text-slate-400">Variables:</span>
                {Object.entries(project.variables).map(([k, v]) => (
                  <span key={k}
                    className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600"
                    title="Inherited by every test case; overridden by a same-named test-case or run-time value.">
                    <span className="font-semibold">{k}</span>
                    <span className="text-slate-400">=</span>
                    <span className="font-mono">{String(v)}</span>
                  </span>
                ))}
              </div>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Button onClick={report} disabled={reporting}>{reporting ? "Generating…" : "Generate report"}</Button>
            {canManage && <Button variant="ghost" onClick={startEdit}>Edit</Button>}
            {canManage && <Button variant="ghost" onClick={remove} disabled={busy}>Archive</Button>}
          </div>
        </div>
      )}

      {/* Health dashboard header (workspace) — pass-rate ring + 7-day trend + KPI chips. Falls
          back to plain stat boxes on the standalone project list (no health computed there). */}
      {health ? (
        <div className="mt-4 flex flex-wrap items-center gap-5 rounded-xl border border-slate-200 bg-gradient-to-br from-white to-slate-50 p-4">
          <StatRing pct={health.passRate} />
          <div className="min-w-[8rem]">
            <div className="text-xs uppercase tracking-wide text-slate-400">Pass rate</div>
            <div className="text-[11px] text-slate-500">{compareCount + monitorCount} test case{compareCount + monitorCount === 1 ? "" : "s"} · {runCount ?? 0} run{(runCount ?? 0) === 1 ? "" : "s"}</div>
          </div>
          <div className="flex flex-col gap-1">
            <div className="text-[10px] uppercase tracking-wide text-slate-400">Last 7 days</div>
            <Sparkline values={health.trend} labels={health.trendLabels} />
          </div>
          <div className="ml-auto flex flex-wrap gap-2">
            <Kpi label="passing" value={health.passing} tone="ok" />
            <Kpi label="differences" value={health.differences} tone="warn" />
            <Kpi label="failed" value={health.failed} tone="bad" />
            {health.running > 0 && <Kpi label="running" value={health.running} tone="info" />}
            {health.notRun > 0 && <Kpi label="not run" value={health.notRun} tone="idle" />}
          </div>
        </div>
      ) : (
        <div className="mt-4 grid grid-cols-3 gap-3">
          {[["Test cases", compareCount + monitorCount], ["Compares", compareCount], ["DQ monitors", monitorCount]].map(([label, val]) => (
            <div key={label as string} className="rounded-xl border border-slate-200 bg-white px-4 py-3.5 shadow-card">
              <div className="text-xs font-medium text-slate-500">{label}</div>
              <div className="mt-1 text-3xl font-bold tabular-nums text-navy">{val}</div>
            </div>
          ))}
        </div>
      )}

      {!hideTestCases && (
        <>
          <div className="mt-4 text-xs font-semibold uppercase text-slate-400">Test cases</div>
          {tcs === null ? (
            <Spinner />
          ) : all.length === 0 ? (
            <p className="mt-2 text-xs text-slate-400">No test cases assigned yet.</p>
          ) : (
            <table className="mt-2 w-full text-sm">
              <thead className="border-b border-slate-200 bg-slate-50/60 text-left text-[11px] uppercase tracking-wide text-slate-400">
                <tr>
                  <SortTh label="Name" columnKey="name" sort={sort} onSort={requestSort} className="px-4 py-2 font-medium" />
                  <SortTh label="Kind" columnKey="kind" sort={sort} onSort={requestSort} className="px-4 py-2 font-medium" />
                  <SortTh label="Source" columnKey="source" sort={sort} onSort={requestSort} className="px-4 py-2 font-medium" />
                  <SortTh label="Layer" columnKey="layer" sort={sort} onSort={requestSort} className="px-4 py-2 font-medium" />
                  <SortTh label="Tags" sortable={false} sort={sort} onSort={requestSort} className="px-4 py-2 font-medium" />
                </tr>
              </thead>
              <tbody>{sortedAll.map(([c, k]) => row(c, k))}</tbody>
            </table>
          )}
        </>
      )}
    </Card>
  );
}

export default function Projects() {
  const navigate = useNavigate();
  const { setPref, can, recents, prefs } = usePrefs();
  const lastViewed = (prefs.projectLastViewed as Record<string, string> | undefined) ?? {};
  const { projects, refresh } = useProjects();
  const canManage = can("manage_projects");
  const [tags, setTags] = useState<Tag[]>([]);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [archived, setArchived] = useState<Project[]>([]);
  const [seeding, setSeeding] = useState(false);
  const [unrunByProject, setUnrunByProject] = useState<Record<string, number>>({});

  const loadTags = () => api.listTags().then((r) => setTags(r.data)).catch(() => setTags([]));
  const loadArchived = () =>
    api.listProjects(true).then((r) => setArchived(r.data.filter((p) => p.archived))).catch(() => setArchived([]));
  useEffect(() => { loadTags(); loadArchived(); }, []);
  useEffect(() => {
    let live = true;
    Promise.all(
      projects.map(async (project) => {
        try {
          const cases = await api.projectTestCases(project.id);
          const unrun = cases.data.compares.filter((item) => item.status === "failed").length;
          return [project.id, unrun] as const;
        } catch {
          return [project.id, 0] as const;
        }
      }),
    ).then((entries) => {
      if (live) setUnrunByProject(Object.fromEntries(entries));
    });
    return () => {
      live = false;
    };
  }, [projects]);

  // Open a project's workspace (its test cases). Remember it as the last one worked on.
  const openProject = (id: string) => {
    setPref("lastProjectId", id);
    navigate(`/validation/projects/${id}`);
  };

  const create = async () => {
    if (!name.trim()) return;
    const r = await api.createProject({ name, description: description || null });
    setAdding(false); setName(""); setDescription("");
    await refresh();
    openProject(r.data.id);
  };

  // One-click demo data so a fresh install isn't empty.
  const seedSample = async () => {
    setSeeding(true);
    try {
      const proj = (await api.createProject({ name: "Sample project", description: "Demo data" })).data;
      const conn = (await api.createConnection({
        source_type: "local", name: "sample-source",
        auth_method: "none",
        coordinates: {
          database: "DEMO", schema: "PUBLIC",
          tables: {
            ORDERS: {
              columns: [{ name: "ORDER_ID", data_type: "NUMBER" }, { name: "AMOUNT", data_type: "NUMBER" }],
              rows: [{ ORDER_ID: 1, AMOUNT: 10 }, { ORDER_ID: 2, AMOUNT: 20 }, { ORDER_ID: 3, AMOUNT: 30 }],
            },
          },
        },
      })).data;
      for (const [kind, nm] of [["data", "orders parity"], ["count", "orders row count"]] as const) {
        const job = (await api.createCompare({
          kind, name: nm, project_id: proj.id,
          source: { connection_id: conn.id, schema: "PUBLIC", table: "ORDERS" },
          target: { connection_id: conn.id, schema: "PUBLIC", table: "ORDERS" },
          options: { key: ["ORDER_ID"] },
        })).data;
        await api.runCompare(job.id);
      }
      await refresh();
      openProject(proj.id);
    } catch (e) {
      window.alert(`Could not create sample data: ${(e as Error).message}`);
    } finally {
      setSeeding(false);
    }
  };

  const { sorted: sortedProjects, sort: projectSort, requestSort: requestProjectSort } = useSort(
    projects,
    {
      project: (p) => p.name,
      test_cases: (p) => (p.compare_count ?? 0) + (p.monitor_count ?? 0),
      last_viewed: (p) => (lastViewed[p.id] ? Date.parse(lastViewed[p.id]) : null),
    },
    { key: "last_viewed", dir: "desc" }, // default: most recently viewed first
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-navy">Data Validation</h1>
          <p className="text-sm text-slate-500">
            Open a project to work on its test cases.
            {!canManage && <span className="ml-1 text-xs text-slate-400">· read-only (admins manage projects)</span>}
          </p>
        </div>
        {canManage && !adding && (
          <div className="flex gap-2">
            {projects.length === 0 && (
              <Button variant="ghost" onClick={seedSample} disabled={seeding}>
                {seeding ? "Adding…" : "Load sample data"}
              </Button>
            )}
            <Button onClick={() => setAdding(true)}>+ New project</Button>
          </div>
        )}
      </div>

      {/* Recently viewed — jump back to a test case, project, or report you were working on */}
      {recents.length > 0 && (
        <div>
          <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-400">Recently viewed</div>
          <div className="flex flex-wrap gap-2">
            {recents.slice(0, 8).map((r) => (
              <Link key={r.path} to={r.path}
                className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm hover:border-teal hover:bg-teal/5">
                <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] uppercase text-slate-500">{r.kind}</span>
                <span className="text-navy">{r.name}</span>
              </Link>
            ))}
          </div>
        </div>
      )}

      {adding && (
        <Card className="space-y-3 p-5">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label="Name">
              <TextInput value={name} onChange={(e) => setName(e.target.value)} placeholder="Finance warehouse migration" />
            </Field>
            <Field label="Description">
              <TextInput value={description} onChange={(e) => setDescription(e.target.value)} />
            </Field>
          </div>
          <div className="flex gap-2">
            <Button onClick={create} disabled={!name.trim()}>Create</Button>
            <button onClick={() => setAdding(false)} className="text-sm text-slate-500 hover:text-navy">Cancel</button>
          </div>
        </Card>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          {projects.length === 0 ? (
            <EmptyState
              title="No projects yet"
              hint={canManage ? "Create a project, or load sample data to explore." : "Ask an admin to create a project."}
            />
          ) : (
            <Card>
              <table className="w-full text-sm">
                <thead className="border-b border-slate-200 bg-slate-50/60 text-left text-[11px] uppercase tracking-wide text-slate-400">
                  <tr>
                    <SortTh label="Project" columnKey="project" sort={projectSort} onSort={requestProjectSort} className="px-5 py-3 font-medium" />
                    <SortTh label="Health" sortable={false} sort={projectSort} onSort={requestProjectSort} className="px-5 py-3 font-medium" />
                    <SortTh label="Test cases" columnKey="test_cases" sort={projectSort} onSort={requestProjectSort} className="px-5 py-3 font-medium" />
                    <SortTh label="Tags" sortable={false} sort={projectSort} onSort={requestProjectSort} className="px-5 py-3 font-medium" />
                    <SortTh label="Last viewed" columnKey="last_viewed" sort={projectSort} onSort={requestProjectSort} className="px-5 py-3 font-medium" />
                    <th className="px-5 py-3"></th>
                  </tr>
                </thead>
                <tbody>
                  {sortedProjects.map((p) => (
                    <tr key={p.id} onClick={() => openProject(p.id)}
                      className="cursor-pointer border-b border-slate-100 last:border-0 hover:bg-slate-50/60">
                      <td className="px-5 py-3">
                        <div className="font-medium text-navy">{p.name}</div>
                        {p.description && <div className="text-xs text-slate-400">{p.description}</div>}
                      </td>
                      <td className="px-5 py-3">
                        {p.health && (p.health.passing + p.health.differences + p.health.failed) > 0 ? (
                          <div className="flex items-center gap-2.5">
                            <StatRing pct={p.health.pass_rate} size={38} stroke={5} />
                            <div className="flex flex-col gap-0.5 text-[11px] leading-tight">
                              {p.health.passing > 0 && <span className="text-emerald-600">● {p.health.passing} passing</span>}
                              {p.health.differences > 0 && <span className="text-rose-600">● {p.health.differences} diff</span>}
                              {p.health.failed > 0 && <span className="text-amber-600">● {p.health.failed} failed</span>}
                            </div>
                          </div>
                        ) : (
                          <span className="text-[11px] text-slate-300">—</span>
                        )}
                      </td>
                      <td className="px-5 py-3 text-xs text-slate-500">
                        <div>{(p.compare_count ?? 0) + (p.monitor_count ?? 0)} total</div>
                        {(unrunByProject[p.id] ?? 0) > 0 && (
                          <div className="mt-1 text-[11px] leading-snug text-slate-500">
                            {unrunByProject[p.id] === 1 ? "1 did not run" : `${unrunByProject[p.id]} did not run`}
                          </div>
                        )}
                      </td>
                      <td className="px-5 py-3">
                        <div className="flex flex-wrap gap-1">{(p.tags || []).map((t) => <TagChip key={t} name={t} />)}</div>
                      </td>
                      <td
                        className="px-5 py-3 text-xs text-slate-500"
                        title={lastViewed[p.id] ? new Date(lastViewed[p.id]).toLocaleString() : "Never opened"}
                      >
                        {timeAgo(lastViewed[p.id])}
                      </td>
                      <td className="px-5 py-3 text-right text-xs text-teal-700">Open →</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}

          {/* Archived projects (soft-deleted) — admins can restore */}
          {canManage && archived.length > 0 && (
            <div className="text-sm">
              <button onClick={() => setShowArchived((v) => !v)} className="text-slate-500 hover:text-navy">
                {showArchived ? "▾" : "▸"} Archived ({archived.length})
              </button>
              {showArchived && (
                <Card className="mt-2">
                  {archived.map((p) => (
                    <div key={p.id} className="flex items-center justify-between border-b border-slate-100 px-5 py-2.5 last:border-0">
                      <span className="text-slate-500">{p.name}</span>
                      <Button variant="ghost" onClick={async () => { await api.restoreProject(p.id); await refresh(); loadArchived(); }}>
                        Restore
                      </Button>
                    </div>
                  ))}
                </Card>
              )}
            </div>
          )}
        </div>
        <TagsManager tags={tags} reload={loadTags} />
      </div>
    </div>
  );
}
