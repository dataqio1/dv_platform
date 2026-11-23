import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { usePrefs } from "../prefs";
import type { Project, ProjectTestCase, Report, ReportTrend, ReportVersion } from "../types";
import { Button, Card, EmptyState, Field, HealthBadge, Spinner, TextInput } from "../components/ui";
import { Combobox } from "../components/Combobox";
import { SortTh, useSort } from "../components/sortable";
import { ProfileCharts, type ProfileTable } from "../components/ProfileCharts";

const TYPES = [
  { key: "data_validation", label: "Data validation" },
  { key: "data_quality", label: "Data quality" },
  { key: "executive", label: "Executive summary" },
  { key: "profile", label: "Data profile" },
];
const TYPE_LABEL: Record<string, string> = Object.fromEntries(TYPES.map((t) => [t.key, t.label]));

type SignOffTable = {
  id: string;
  table: string;
  status: string;
  src_count?: number | null;
  dst_count?: number | null;
  exception_keys?: string[];
};
type SignOffDoc = { ran_by?: string | null; ran_at?: string | null; tables?: SignOffTable[] };

function SignOffView({ signOff }: { signOff: SignOffDoc }) {
  const when = signOff.ran_at ? new Date(signOff.ran_at).toLocaleString() : "";
  const tables = signOff.tables ?? [];
  return (
    <div className="mt-4 space-y-2">
      <p className="text-sm text-slate-600">
        Ran by <span className="font-medium text-navy">{signOff.ran_by || "unknown"}</span>
        {when ? ` · ${when}` : ""}
      </p>
      {tables.map((t) =>
        t.status === "pass" ? (
          <div key={t.id} className="text-sm text-slate-600">
            <span className="font-medium text-navy">{t.table}</span> matches
            {t.src_count != null ? ` · ${t.src_count.toLocaleString()} rows` : ""}
          </div>
        ) : (
          <div key={t.id} className="rounded-lg border border-rose-100 px-3 py-2 text-sm">
            <Link to={`/validation/${t.id}`} className="font-medium text-teal-700 hover:underline">
              {t.table}
            </Link>
            <span className="text-slate-500">
              {" "}
              differs · source {t.src_count ?? "—"} vs target {t.dst_count ?? "—"}
            </span>
            {(t.exception_keys ?? []).length > 0 && (
              <div className="mt-1 font-mono text-xs text-slate-600">{t.exception_keys!.join(" · ")}</div>
            )}
          </div>
        )
      )}
    </div>
  );
}

function headline(r: Report): number | null {
  const s = r.summary || {};
  const v = (s.pass_rate_pct ?? s.health_score_pct) as number | undefined;
  return v ?? null;
}

function omittedRunSentence(report: Report, compares: ProjectTestCase[]): string | null {
  if (report.report_type !== "data_validation") return null;
  const failed = compares.filter((item) => item.status === "failed");
  const counted = Number(report.summary?.failed ?? 0);
  const missing = failed.length - (Number.isFinite(counted) ? counted : 0);
  if (missing <= 0) return null;
  if (counted === 0 && failed.length === 1) {
    const name = failed[0].name || "A compare";
    return `${name} did not run. This number does not include it.`;
  }
  if (counted === 0) {
    return `${failed.length} compares did not run. This number does not include them.`;
  }
  return missing === 1
    ? "A compare did not run. This number does not include it."
    : `${missing} compares did not run. This number does not include them.`;
}

function statusBadge(r: Report, stale: boolean) {
  if (r.status === "ready") {
    const h = headline(r);
    let tone: "unknown" | "healthy" | "warning" | "critical" =
      h === null ? "unknown" : h >= 95 ? "healthy" : h >= 70 ? "warning" : "critical";
    if (stale && tone === "healthy") tone = "unknown";
    return <HealthBadge status={tone} label={h === null ? "ready" : `${h}%`} />;
  }
  if (r.status === "failed") return <HealthBadge status="critical" label="Failed" />;
  return <HealthBadge status="unknown" label="Draft" />;
}

function ReportDetail({
  report,
  omitted,
  onChange,
}: {
  report: Report;
  omitted: string | null;
  onChange: () => void;
}) {
  const [full, setFull] = useState<Report | null>(null);
  const [versions, setVersions] = useState<ReportVersion[]>([]);
  const [trend, setTrend] = useState<ReportTrend | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => {
    api.getReport(report.id).then((r) => setFull(r.data));
    api.reportVersions(report.id).then((r) => setVersions(r.data)).catch(() => setVersions([]));
    api.reportTrend(report.id).then((r) => setTrend(r.data)).catch(() => setTrend(null));
  };
  useEffect(load, [report.id]);

  const generate = async () => {
    setBusy(true);
    try {
      await api.generateReport(report.id);
      load();
      onChange();
    } finally {
      setBusy(false);
    }
  };

  const deliver = async () => {
    setBusy(true);
    try {
      const r = await api.deliverReport(report.id);
      window.alert(`Delivered to ${r.data.delivered.length} recipient(s).`);
      load();
      onChange();
    } finally {
      setBusy(false);
    }
  };

  const s = (full?.summary ?? report.summary ?? {}) as Record<string, number>;
  const stat = (label: string, key: string, suffix = "") =>
    s[key] !== undefined ? (
      <div className="rounded-lg border border-slate-200 p-3">
        <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
        <div className="mt-1 text-xl font-bold text-navy">
          {s[key]}
          {suffix}
        </div>
      </div>
    ) : null;

  return (
    <Card className="p-5">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-lg font-bold text-navy">{report.name}</div>
          <div className="text-xs text-slate-500">
            {TYPE_LABEL[report.report_type] ?? report.report_type}
            {report.latest_version > 0 ? ` · v${report.latest_version}` : " · not generated"}
            {trend?.delta != null && (
              <span className={trend.delta >= 0 ? "ml-2 text-emerald-600" : "ml-2 text-rose-600"}>
                {trend.delta >= 0 ? "▲" : "▼"} {Math.abs(trend.delta)}% vs prev
              </span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button onClick={generate} disabled={busy}>
            {busy ? "Generating…" : report.latest_version > 0 ? "Regenerate" : "Generate"}
          </Button>
        </div>
      </div>

      {omitted && <p className="mt-3 text-sm text-slate-600">{omitted}</p>}

      {report.status === "ready" && (
        <>
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            {stat("Pass rate", "pass_rate_pct", "%")}
            {stat("Health", "health_score_pct", "%")}
            {stat("Test cases", "total_test_cases")}
            {stat("Passed", "passed")}
            {stat("Failed", "failed")}
            {stat("Rules", "total_rules")}
            {stat("Open incidents", "open_incidents")}
            {stat("Tables profiled", "tables_profiled")}
            {stat("Columns", "columns_profiled")}
          </div>

          {full?.document?.sign_off ? (
            <SignOffView signOff={full.document.sign_off as SignOffDoc} />
          ) : null}

          {report.report_type === "profile" && full?.document?.tables && (
            <div className="mt-4 space-y-5">
              {(full.document.tables as ProfileTable[]).map((t) => (
                <div key={t.table}>
                  <div className="mb-2 text-sm font-semibold text-navy">
                    {t.table} <span className="text-xs text-slate-400">· {t.row_count.toLocaleString()} rows</span>
                  </div>
                  <ProfileCharts table={t} />
                </div>
              ))}
            </div>
          )}

          <div className="mt-4 flex flex-wrap items-center gap-2 text-xs">
            <span className="text-slate-400">Export:</span>
            {["json", "csv", "html", "pdf"].map((f) => (
              <a key={f} href={api.reportExportUrl(report.id, f)} target="_blank" rel="noreferrer"
                className="rounded bg-slate-100 px-2 py-1 font-medium text-slate-600 hover:bg-slate-200">
                {f.toUpperCase()}
              </a>
            ))}
            {report.schedule_cron && (
              <button onClick={deliver} disabled={busy}
                className="ml-auto rounded bg-teal px-2 py-1 font-medium text-white disabled:opacity-40">
                Deliver now
              </button>
            )}
          </div>

          {versions.length > 0 && (
            <div className="mt-4">
              <div className="mb-1 text-xs font-semibold uppercase text-slate-400">Versions (immutable)</div>
              <div className="space-y-1 text-xs">
                {versions.map((v) => (
                  <div key={v.id} className="flex items-center justify-between rounded border border-slate-100 px-2 py-1">
                    <span className="font-mono text-slate-600">v{v.version}</span>
                    <span className="text-slate-500">
                      {(v.summary?.pass_rate_pct as number) ?? (v.summary?.health_score_pct as number) ?? "—"}%
                    </span>
                    <span className="text-slate-400">
                      {v.generated_at ? new Date(v.generated_at).toLocaleString() : ""}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
      {report.status === "failed" && (
        <div className="mt-3 rounded-lg bg-rose-50 p-3 text-xs text-rose-700">{report.error}</div>
      )}
    </Card>
  );
}

export default function Reports() {
  const navigate = useNavigate();
  const { recordRecent, loaded: prefsLoaded } = usePrefs();
  const { reportId } = useParams(); // deep-linkable: /reports/:reportId
  const selected = reportId ?? null;
  const select = (id: string) => navigate(`/reports/${id}`);
  const [reports, setReports] = useState<Report[] | null>(null);
  const [comparesByProject, setComparesByProject] = useState<Record<string, ProjectTestCase[]>>({});
  const [projects, setProjects] = useState<Project[]>([]);
  const [connections, setConnections] = useState<{ id: string; name: string }[]>([]);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [type, setType] = useState("data_validation");
  const [projectId, setProjectId] = useState("");
  const [cron, setCron] = useState("");
  const [emails, setEmails] = useState("");
  const [slack, setSlack] = useState("");
  const [pConn, setPConn] = useState("");
  const [pSchema, setPSchema] = useState("");
  const [pTables, setPTables] = useState("");

  const load = () => api.listReports().then((r) => setReports(r.data)).catch(() => setReports([]));
  useEffect(() => {
    if (!reports) return;
    const ids = [...new Set(reports.map((report) => report.project_id).filter((id): id is string => Boolean(id)))];
    let live = true;
    Promise.all(
      ids.map(async (id) => {
        try {
          const cases = await api.projectTestCases(id);
          return [id, cases.data.compares] as const;
        } catch {
          return [id, [] as ProjectTestCase[]] as const;
        }
      }),
    ).then((entries) => {
      if (live) setComparesByProject(Object.fromEntries(entries));
    });
    return () => {
      live = false;
    };
  }, [reports]);
  useEffect(() => {
    load();
    api.listProjects().then((r) => setProjects(r.data)).catch(() => setProjects([]));
    api.listConnections().then((r) => setConnections(r.data)).catch(() => setConnections([]));
  }, []);

  const create = async () => {
    if (!name.trim()) return;
    const delivery: Record<string, string[]> = {};
    const em = emails.split(",").map((x) => x.trim()).filter(Boolean);
    const sl = slack.split(",").map((x) => x.trim()).filter(Boolean);
    if (em.length) delivery.email = em;
    if (sl.length) delivery.slack = sl;
    const spec: Record<string, unknown> =
      type === "profile"
        ? {
            connection_id: pConn,
            schema: pSchema || undefined,
            tables: pTables.split(",").map((x) => x.trim()).filter(Boolean),
          }
        : {};
    const r = await api.createReport({
      name, report_type: type, project_id: projectId || null,
      schedule_cron: cron || null, delivery, spec,
    });
    setAdding(false);
    setName("");
    setProjectId("");
    setCron("");
    setEmails("");
    setSlack("");
    await load();
    select(r.data.id);
  };

  const sel = reports?.find((r) => r.id === selected) ?? null;
  // Remember the viewed report in "recently viewed" (after prefs load, to avoid clobbering the list).
  useEffect(() => {
    if (sel && prefsLoaded) recordRecent({ kind: "Report", id: sel.id, name: sel.name, path: `/reports/${sel.id}` });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sel?.id, sel?.name, prefsLoaded]);

  const { sorted: sortedReports, sort, requestSort } = useSort(reports ?? [], {
    name: (r) => r.name,
    type: (r) => TYPE_LABEL[r.report_type] ?? r.report_type,
    status: (r) => r.status,
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-navy">Reports</h1>
          <p className="text-sm text-slate-500">
            Standing, versioned reports over your projects — generate, trend, and export.
          </p>
        </div>
        {!adding && <Button onClick={() => setAdding(true)}>+ New report</Button>}
      </div>

      {adding && (
        <Card className="space-y-3 p-5">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Field label="Name">
              <TextInput value={name} onChange={(e) => setName(e.target.value)} placeholder="Nightly reconciliation" />
            </Field>
            <Field label="Type">
              <select value={type} onChange={(e) => setType(e.target.value)}
                className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm">
                {TYPES.map((t) => <option key={t.key} value={t.key}>{t.label}</option>)}
              </select>
            </Field>
            <Field label="Project" hint="optional — scopes the report">
              <Combobox
                value={projectId}
                onChange={setProjectId}
                allowCustom={false}
                placeholder="All test cases"
                options={[{ value: "", label: "All test cases" }, ...projects.map((p) => ({ value: p.id, label: p.name }))]}
              />
            </Field>
          </div>

          {type === "profile" && (
            <div className="grid grid-cols-1 gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3 sm:grid-cols-3">
              <Field label="Connection">
                <Combobox
                  value={pConn}
                  onChange={setPConn}
                  allowCustom={false}
                  placeholder="Select…"
                  options={connections.map((c) => ({ value: c.id, label: c.name }))}
                />
              </Field>
              <Field label="Schema">
                <TextInput value={pSchema} onChange={(e) => setPSchema(e.target.value)} placeholder="PUBLIC" />
              </Field>
              <Field label="Tables" hint="comma-separated">
                <TextInput value={pTables} onChange={(e) => setPTables(e.target.value)} placeholder="ORDERS, CUSTOMERS" />
              </Field>
            </div>
          )}

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Field label="Schedule (cron)" hint="optional — e.g. 0 7 * * *">
              <TextInput value={cron} onChange={(e) => setCron(e.target.value)} placeholder="0 7 * * *" />
            </Field>
            <Field label="Email recipients" hint="comma-separated">
              <TextInput value={emails} onChange={(e) => setEmails(e.target.value)} placeholder="lead@acme.io" />
            </Field>
            <Field label="Slack channels" hint="comma-separated">
              <TextInput value={slack} onChange={(e) => setSlack(e.target.value)} placeholder="#data-health" />
            </Field>
          </div>

          <div className="flex gap-2">
            <Button onClick={create} disabled={!name.trim() || (type === "profile" && (!pConn || !pTables.trim()))}>
              Create
            </Button>
            <button onClick={() => setAdding(false)} className="text-sm text-slate-500 hover:text-navy">Cancel</button>
          </div>
        </Card>
      )}

      {reports === null ? (
        <Spinner />
      ) : reports.length === 0 ? (
        <EmptyState title="No reports yet" hint="Create a report to aggregate your validations and DQ rules." />
      ) : (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <Card>
            <table className="w-full text-sm">
              <thead className="border-b border-slate-200 text-left text-xs uppercase text-slate-400">
                <tr>
                  <SortTh label="Name" columnKey="name" sort={sort} onSort={requestSort} className="px-5 py-3 font-medium" />
                  <SortTh label="Type" columnKey="type" sort={sort} onSort={requestSort} className="px-5 py-3 font-medium" />
                  <SortTh label="Status" columnKey="status" sort={sort} onSort={requestSort} className="px-5 py-3 font-medium" />
                </tr>
              </thead>
              <tbody>
                {sortedReports.map((r) => {
                  const omitted = omittedRunSentence(r, r.project_id ? comparesByProject[r.project_id] ?? [] : []);
                  return (
                  <tr key={r.id} onClick={() => select(r.id)}
                    className={`cursor-pointer border-b border-slate-100 last:border-0 hover:bg-slate-50/60 ${
                      selected === r.id ? "bg-teal/5" : ""
                    }`}>
                    <td className="px-5 py-3 font-medium text-navy">{r.name}</td>
                    <td className="px-5 py-3 text-xs text-slate-500">{TYPE_LABEL[r.report_type] ?? r.report_type}</td>
                    <td className="px-5 py-3">
                      {statusBadge(r, Boolean(omitted))}
                      {omitted && <div className="mt-1 max-w-52 text-[11px] leading-snug text-slate-500">{omitted}</div>}
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </Card>
          {sel ? (
            <ReportDetail
              report={sel}
              omitted={omittedRunSentence(sel, sel.project_id ? comparesByProject[sel.project_id] ?? [] : [])}
              onChange={load}
            />
          ) : (
            <EmptyState title="Select a report" hint="Pick a report to generate, view versions, and export." />
          )}
        </div>
      )}
    </div>
  );
}
