import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import type { AlertPolicy, AuditEntry, ColumnStat, Health, HistoryPoint, Incident, Monitor, TableSummary } from "../types";
import { DEFAULT_ALERT_POLICY } from "../types";
import { Button, Card, EmptyState, Field, HealthBadge, honestyBadge, Spinner, TextInput } from "../components/ui";
import { Tabs } from "../components/vex";
import { appTz } from "../tz";
import { TrendChart } from "../components/TrendChart";
import { AlertPolicyFields } from "../components/AlertPolicyFields";
import { Combobox } from "../components/Combobox";
import { RulesPanel } from "../components/RulesPanel";
import { SuggestedRules } from "../components/SuggestedRules";
import { MetricsPanel } from "../components/MetricsPanel";
import { SchedulePicker } from "../components/SchedulePicker";
import { SortTh, useSort, type SortValue } from "../components/sortable";

// Worst-first health rank so sorting a status column groups problems at the top (asc).
const HEALTH_RANK: Record<string, number> = { critical: 0, warning: 1, unknown: 2, healthy: 3 };

// Resolves the active table's load pattern (drives the rule suggestions) and reports it up.
function ActiveTablePattern({
  monitorId,
  tableRef,
  onPattern,
}: {
  monitorId: string;
  tableRef: string;
  onPattern: (p: string) => void;
}) {
  useEffect(() => {
    let ignore = false;
    api
      .tablePillars(monitorId, tableRef)
      .then((r) => !ignore && onPattern(r.data.load_pattern ?? ""))
      .catch(() => !ignore && onPattern(""));
    return () => {
      ignore = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [monitorId, tableRef]);
  return null;
}

export const PILLARS = ["freshness", "volume", "schema", "quality"] as const;

function incidentHref(monitorId: string, i: Incident): string {
  return i.table_ref
    ? `/observability/${monitorId}/tables/${encodeURIComponent(i.table_ref)}`
    : "/incidents";
}

// Figma "Monitor health" card: checks passing + coverage bar + per-pillar passing counts.
function MonitorHealthCard({ tables }: { tables: TableSummary[] }) {
  if (tables.length === 0) return null;
  const rows = PILLARS.map((p) => ({
    p,
    total: tables.length,
    pass: tables.filter((t) => t.pillars[p] === "healthy").length,
  }));
  const total = rows.reduce((a, r) => a + r.total, 0);
  const pass = rows.reduce((a, r) => a + r.pass, 0);
  const pct = total ? Math.round((pass / total) * 100) : 0;
  return (
    <Card className="p-5">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-navy">Monitor health</h2>
        <div className="text-sm">
          <span className="text-2xl font-bold text-navy">{pass}</span>
          <span className="text-slate-400"> / {total} checks passing</span>
        </div>
      </div>
      <div className="mt-3 flex h-2 overflow-hidden rounded-full bg-slate-100">
        <div className="bg-emerald-500" style={{ width: `${pct}%` }} />
        {pct < 100 && <div className="bg-amber-400" style={{ width: `${100 - pct}%` }} />}
      </div>
      <div className="mt-4 divide-y divide-slate-100">
        {rows.map((r) => (
          <div key={r.p} className="flex items-center justify-between py-2 text-sm">
            <span className="capitalize text-slate-600">{r.p}</span>
            <span className={r.pass < r.total ? "font-medium text-amber-600" : "font-medium text-emerald-600"}>
              {r.pass} / {r.total} passing
            </span>
          </div>
        ))}
      </div>
    </Card>
  );
}

export type Standing = "broken" | "setup" | "healthy";

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export function volumeGapSentence(points: { ts: string }[]): string | null {
  if (points.length === 0) return null;
  const ordered = [...points].sort((a, b) => a.ts.localeCompare(b.ts));
  const latest = ordered[ordered.length - 1];
  const weekdayOf = (iso: string) => {
    const [year, month, day] = iso.slice(0, 10).split("-").map(Number);
    return new Date(Date.UTC(year, month - 1, day)).getUTCDay();
  };
  const latestDay = weekdayOf(latest.ts);
  const prior = ordered.slice(0, -1).filter((point) => weekdayOf(point.ts) === latestDay).length;
  const missing = 3 - prior;
  if (missing <= 0) return null;
  const day = WEEKDAYS[(latestDay + 6) % 7];
  const noun = missing === 1 ? "scan" : "scans";
  return `need ${missing} more ${day} ${noun} before volume can alert`;
}

export async function withVolumeNotes(monitorId: string, tables: TableSummary[]): Promise<TableSummary[]> {
  return Promise.all(
    tables.map(async (table) => {
      if (table.pillars?.volume !== "healthy") return table;
      try {
        const points = (await api.pillarHistory(monitorId, table.table_ref, "volume")).data.points;
        const note = volumeGapSentence(points);
        return note ? { ...table, volume_note: note } : table;
      } catch {
        return table;
      }
    }),
  );
}

const WATERMARK_NAMES = [
  "load_ts",
  "ingested_at",
  "loaded_at",
  "inserted_at",
  "created_at",
  "updated_at",
  "modified_at",
  "event_time",
  "orderdate",
  "order_date",
];

export function suggestWatermark(columns: ColumnStat[] | null): { column: string; type: string } | null {
  if (!columns) return null;
  let best: { rank: number; column: string; type: string } | null = null;
  for (const col of columns) {
    const raw = col.column;
    if (!raw) continue;
    const low = raw.toLowerCase();
    const forms = [low];
    if (low.length > 2 && /[a-z]/i.test(low[0]) && low[1] === "_") forms.push(low.slice(2));
    let rank: number | null = null;
    for (const form of forms) {
      const found = WATERMARK_NAMES.indexOf(form);
      if (found >= 0 && (rank === null || found < rank)) rank = found;
    }
    if (rank === null) continue;
    const dtype = (col.data_type || "").toUpperCase();
    const type =
      (dtype.includes("DATE") && !dtype.includes("TIME")) || low.endsWith("date") ? "date" : "timestamp";
    if (!best || rank < best.rank) best = { rank, column: raw, type };
  }
  return best ? { column: best.column, type: best.type } : null;
}

export function freshnessGapSentence(
  headline: string | null | undefined,
  watermark: string | null | undefined,
  suggestion: { column: string } | null,
): string | null {
  if (headline && headline !== "Not configured yet") return headline;
  if (watermark) return `No scan has read ${watermark}`;
  if (suggestion) return `${suggestion.column} looks like the load time`;
  return null;
}

export async function withFreshnessNotes(monitorId: string, tables: TableSummary[]): Promise<TableSummary[]> {
  return Promise.all(
    tables.map(async (table) => {
      if (table.pillars?.freshness !== "unknown") return table;
      try {
        const detail = (await api.tablePillars(monitorId, table.table_ref)).data;
        const card = detail.pillars.find((pillar) => pillar.pillar === "freshness");
        const watermark = detail.watermark_column;
        let suggestion = detail.suggested_watermark ?? null;
        if (!watermark && !suggestion && (!card || card.headline === "Not configured yet")) {
          const cols = (await api.tableColumns(monitorId, table.table_ref)).data;
          suggestion = suggestWatermark(cols);
        }
        const note = freshnessGapSentence(card?.headline, watermark, suggestion);
        return note ? { ...table, freshness_note: note } : table;
      } catch {
        return table;
      }
    }),
  );
}

export function qualityGapSentence(
  pillars: { pillar: string; headline: string }[],
  queryShape: string | null | undefined,
): string | null {
  const quality = pillars.find((card) => card.pillar === "quality");
  if (!quality) return null;
  if (quality.headline && quality.headline !== "Not configured yet") return quality.headline;
  const scanned = pillars.some(
    (card) =>
      (card.pillar === "volume" || card.pillar === "schema") &&
      Boolean(card.headline) &&
      card.headline !== "Not configured yet",
  );
  if (!scanned) return null;
  return queryShape === "check" ? "The last check did not score columns" : "No scan has scored quality";
}

export async function withQualityNotes(monitorId: string, tables: TableSummary[]): Promise<TableSummary[]> {
  return Promise.all(
    tables.map(async (table) => {
      if (table.pillars?.quality !== "unknown") return table;
      try {
        const detail = (await api.tablePillars(monitorId, table.table_ref)).data;
        const note = qualityGapSentence(detail.pillars, detail.query_shape);
        return note ? { ...table, quality_note: note } : table;
      } catch {
        return table;
      }
    }),
  );
}

export async function annotateTables(monitorId: string, tables: TableSummary[]): Promise<TableSummary[]> {
  const typed = await withoutUntypedSchema(monitorId, tables);
  const withVolume = await withVolumeNotes(monitorId, typed);
  const withFreshness = await withFreshnessNotes(monitorId, withVolume);
  return withQualityNotes(monitorId, withFreshness);
}

export const TYPE_GAP = "Types were not recorded, so a type change would be missed";

export async function withoutUntypedSchema(
  monitorId: string,
  tables: TableSummary[],
): Promise<TableSummary[]> {
  return Promise.all(
    tables.map(async (table) => {
      const status = table.pillars?.schema;
      if (status !== "healthy" && status !== "unknown") return table;
      try {
        const cols = (await api.tableColumns(monitorId, table.table_ref)).data;
        if (cols.length === 0 || cols.some((column) => column.data_type)) return table;
        return {
          ...table,
          pillars: { ...table.pillars, schema: "unknown" },
          schema_note: TYPE_GAP,
        };
      } catch {
        return table;
      }
    }),
  );
}

export function tableStanding(t: TableSummary): Standing {
  const badge = honestyBadge(
    t.health,
    t.score,
    PILLARS.map((p) => t.pillars[p]),
  );
  if (badge.status === "critical" || badge.status === "warning") return "broken";
  if (badge.status === "unknown") return "setup";
  return "healthy";
}

export function fmtWhen(iso: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  const mins = Math.floor((Date.now() - then) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 48) return `${hrs}h ago`;
  return new Date(iso).toLocaleDateString(undefined, { timeZone: appTz(), month: "short", day: "numeric" });
}

export function scheduleSentence(monitor: {
  schedule_cron?: string | null;
  schedule_label?: string | null;
  next_run?: string | null;
}): string {
  if (!monitor.schedule_cron) return "Nothing is scheduled";
  const label = monitor.schedule_label || monitor.schedule_cron;
  return monitor.next_run ? `${label} · next ${fmtWhen(monitor.next_run)}` : label;
}

export const CHECKS_OFF =
  "Checks are not running on this server. The last result stays in place until someone runs a check.";

export function scopeLabel(monitor: Monitor): string {
  const db = monitor.scope?.database;
  const schema = monitor.scope?.schema;
  if (db && schema) return `${db}.${schema}`;
  return schema || db || "All tables on the connection";
}

const STANDING_RANK: Record<Standing, number> = { broken: 0, setup: 1, healthy: 2 };
const FILTERS: { id: "all" | Standing; label: string }[] = [
  { id: "all", label: "All" },
  { id: "broken", label: "Broken" },
  { id: "setup", label: "Setting up" },
  { id: "healthy", label: "Healthy" },
];

function countTone(standing: Standing): Health {
  if (standing === "broken") return "critical";
  if (standing === "setup") return "unknown";
  return "healthy";
}

const FIELD_LABEL: Record<string, string> = {
  name: "Name",
  description: "Description",
  status: "Status",
  watermark_column: "Watermark",
  watermark_type: "Watermark type",
  load_type: "Load type",
  project_id: "Project",
  source_system: "Source system",
  layer: "Layer",
  tags: "Tags",
};

function valueText(value: unknown): string {
  if (value == null || value === "") return "empty";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "empty";
  return String(value);
}

export function auditSentences(entry: AuditEntry): string[] {
  const changes = entry.detail?.changes;
  if (changes && typeof changes === "object") {
    return Object.entries(changes as Record<string, { from?: unknown; to?: unknown }>).map(
      ([field, change]) =>
        `${FIELD_LABEL[field] ?? field} changed from ${valueText(change?.from)} to ${valueText(change?.to)}`,
    );
  }
  if (entry.action === "monitor.create") {
    const name = entry.detail?.name;
    return [name ? `Created ${valueText(name)}` : "Created a monitor"];
  }
  if (entry.action === "monitor.delete") {
    const name = entry.detail?.name;
    return [name ? `Removed ${valueText(name)}` : "Removed a monitor"];
  }
  return [entry.action];
}

export default function MonitorDetail() {
  const { monitorId = "" } = useParams();
  const navigate = useNavigate();
  const [monitor, setMonitor] = useState<Monitor | null>(null);
  const [tables, setTables] = useState<TableSummary[] | null>(null);
  const [audit, setAudit] = useState<AuditEntry[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<(typeof FILTERS)[number]["id"]>("all");
  const [editing, setEditing] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [scheduleCron, setScheduleCron] = useState<string | null>(null);
  const [lookbackDays, setLookbackDays] = useState(30);
  const [alertPolicy, setAlertPolicy] = useState<AlertPolicy>(DEFAULT_ALERT_POLICY);
  const [confirmName, setConfirmName] = useState("");
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [confirmScan, setConfirmScan] = useState(false);
  const [formErr, setFormErr] = useState<string | null>(null);
  const [checksOff, setChecksOff] = useState(false);
  // Which table the monitor-level DQ rules panel is editing (defaults to the first table).
  const [rulesTable, setRulesTable] = useState("");
  const [rulesTablePattern, setRulesTablePattern] = useState("");
  const [metricsRefresh, setMetricsRefresh] = useState(0);
  const [tab, setTab] = useState<"overview" | "checks" | "activity" | "settings">("overview");
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [volumeHist, setVolumeHist] = useState<HistoryPoint[]>([]);
  const [repRef, setRepRef] = useState("");

  function loadAudit(id: string) {
    api.listAudit("monitor", id).then((r) => setAudit(r.data)).catch(() => setAudit([]));
  }

  useEffect(() => {
    setMonitor(null);
    setTables(null);
    setErr(null);
    setAudit(null);
    setEditing(false);
    setRemoving(false);
    api.getMonitor(monitorId).then((r) => setMonitor(r.data)).catch((e) => setErr((e as Error).message));
    api.systemInfo().then((r) => setChecksOff(!r.data.scheduler.enabled)).catch(() => setChecksOff(false));
    api
      .listTables(monitorId)
      .then((r) => annotateTables(monitorId, r.data))
      .then(setTables)
      .catch(() => setTables([]));
    api.listIncidents(true)
      .then((r) => setIncidents(r.data.filter((i) => i.monitor_id === monitorId)))
      .catch(() => setIncidents([]));
    loadAudit(monitorId);
  }, [monitorId]);

  // Overview volume chart: the worst-standing table drives the headline trend.
  useEffect(() => {
    if (!tables || tables.length === 0) return;
    const rep = [...tables].sort((a, b) => STANDING_RANK[tableStanding(a)] - STANDING_RANK[tableStanding(b)])[0];
    setRepRef(rep.table_ref);
    api.pillarHistory(monitorId, rep.table_ref, "volume", monitor?.lookback_days ?? 30)
      .then((r) => setVolumeHist(r.data.points)).catch(() => setVolumeHist([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tables]);

  async function saveDetails() {
    setBusy(true);
    setFormErr(null);
    try {
      const saved = await api.updateMonitor(monitorId, {
        name: name.trim(),
        description,
        schedule_cron: scheduleCron ?? "",
        lookback_days: lookbackDays,
        alert_policy: alertPolicy,
      });
      setMonitor(saved.data);
      setEditing(false);
      loadAudit(monitorId);
    } catch (e) {
      setFormErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function removeMonitor() {
    setBusy(true);
    setFormErr(null);
    try {
      await api.deleteMonitor(monitorId);
      navigate("/observability");
    } catch (e) {
      setFormErr((e as Error).message);
      setBusy(false);
    }
  }

  const counts = useMemo(() => {
    const c = { broken: 0, setup: 0, healthy: 0 };
    for (const t of tables ?? []) c[tableStanding(t)] += 1;
    return c;
  }, [tables]);

  const openInc = incidents.find((i) => i.status !== "resolved") ?? null;

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (tables ?? [])
      .filter((t) => (filter === "all" ? true : tableStanding(t) === filter))
      .filter((t) => !q || t.table_ref.toLowerCase().includes(q))
      .slice()
      .sort(
        (a, b) =>
          STANDING_RANK[tableStanding(a)] - STANDING_RANK[tableStanding(b)] ||
          a.table_ref.localeCompare(b.table_ref),
      );
  }, [tables, query, filter]);

  // Clickable column sort layered on top of the default (standing → name) order: no column
  // selected keeps that default; clicking a header sorts by it. Status columns sort by health.
  const sortAccessors: Record<string, (t: TableSummary) => SortValue> = useMemo(
    () => ({
      table: (t) => t.table_ref.split(".").slice(-1)[0],
      overall: (t) => STANDING_RANK[tableStanding(t)],
      freshness: (t) => HEALTH_RANK[(t.pillars.freshness as string) ?? "unknown"],
      volume: (t) => HEALTH_RANK[(t.pillars.volume as string) ?? "unknown"],
      schema: (t) => HEALTH_RANK[(t.pillars.schema as string) ?? "unknown"],
      quality: (t) => HEALTH_RANK[(t.pillars.quality as string) ?? "unknown"],
      last_scanned: (t) => t.last_scanned ?? null,
      scans: (t) => t.scan_count,
    }),
    [],
  );
  const { sorted: sortedTables, sort, requestSort } = useSort(shown, sortAccessors);

  if (err) return <p className="text-sm text-rose-600">{err}</p>;
  if (!monitor || tables === null) return <Spinner />;

  const scoped = monitor.scope?.tables?.length ?? 0;
  const scanRefs = (monitor.scope?.tables ?? []).map((t) =>
    [monitor.scope?.database, monitor.scope?.schema, t].filter(Boolean).join("."),
  );

  async function checkNow() {
    setChecking(true);
    setFormErr(null);
    try {
      await api.runMonitor(monitorId);
      const r = await api.listTables(monitorId);
      setTables(await annotateTables(monitorId, r.data));
    } catch (e) {
      setFormErr((e as Error).message);
    } finally {
      setChecking(false);
    }
  }

  async function fullProfile() {
    setBusy(true);
    setFormErr(null);
    try {
      for (const ref of scanRefs) await api.profileTable(monitorId, ref);
      const r = await api.listTables(monitorId);
      setTables(await annotateTables(monitorId, r.data));
      setConfirmScan(false);
    } catch (e) {
      setFormErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <Link to="/observability" className="text-sm text-slate-400 hover:text-slate-600">
          ← Monitors
        </Link>
        <div className="mt-1 flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="text-2xl font-bold text-navy">{monitor.name}</h1>
              <span
                className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${
                  counts.broken
                    ? "bg-rose-50 text-rose-700 ring-rose-200"
                    : counts.setup
                      ? "bg-violet-50 text-violet-700 ring-violet-200"
                      : "bg-emerald-50 text-emerald-700 ring-emerald-200"
                }`}
              >
                <span className={`h-1.5 w-1.5 rounded-full ${counts.broken ? "bg-rose-500" : counts.setup ? "bg-violet-500" : "bg-emerald-500"}`} />
                {counts.broken ? "Needs attention" : counts.setup ? "Learning" : "Healthy"}
              </span>
            </div>
            {monitor.description ? (
              <p className="mt-1 max-w-3xl text-sm text-slate-600">{monitor.description}</p>
            ) : (
              <p className="mt-1 text-sm text-slate-400">No description</p>
            )}
            <p className="mt-1 text-xs text-slate-500">
              {scheduleSentence(monitor)}
              {monitor.last_run_at ? ` · last run ${fmtWhen(monitor.last_run_at)}` : ""}
              {` · analyzing last ${monitor.lookback_days ?? 30} days`}
              {monitor.alert_policy?.freshness?.source === "file_arrival"
                ? " · freshness from file arrival"
                : ""}
            </p>
            {checksOff && <p className="mt-1 max-w-3xl text-sm text-amber-800">{CHECKS_OFF}</p>}
          </div>
          {!editing && !removing && (
            <div className="flex shrink-0 gap-2">
              <Button
                variant="ghost"
                onClick={() => {
                  setFormErr(null);
                  setEditing(false);
                  setRemoving(false);
                  setConfirmScan(true);
                }}
              >
                Full profile
              </Button>
              <Button onClick={checkNow} disabled={checking}>
                {checking ? "Checking…" : "Check now"}
              </Button>
              <Button
                variant="ghost"
                onClick={() => navigate(`/observability/${monitor.id}/edit`)}
              >
                Edit
              </Button>
              <Button
                variant="ghost"
                className="text-rose-700 ring-rose-200 hover:bg-rose-50"
                onClick={() => {
                  setConfirmName("");
                  setFormErr(null);
                  setRemoving(true);
                }}
              >
                Remove
              </Button>
            </div>
          )}
        </div>
        {confirmScan && (
          <Card className="mt-4 max-w-xl space-y-3 p-4">
            <p className="text-sm font-medium text-navy">Full profile scans every column of these tables.</p>
            <ul className="list-disc space-y-1 pl-5 text-sm text-slate-600">
              {scanRefs.length === 0 ? (
                <li>No tables in this monitor</li>
              ) : (
                scanRefs.map((ref) => <li key={ref}>{ref}</li>)
              )}
            </ul>
            {formErr && <p className="text-sm text-rose-600">{formErr}</p>}
            <div className="flex gap-2">
              <Button onClick={fullProfile} disabled={busy || scanRefs.length === 0}>
                {busy ? "Scanning…" : "Scan these tables"}
              </Button>
              <Button variant="ghost" onClick={() => setConfirmScan(false)} disabled={busy}>
                Cancel
              </Button>
            </div>
          </Card>
        )}
        {editing && (
          <Card className="mt-4 max-w-xl space-y-3 p-4">
            <Field label="Name">
              <TextInput value={name} onChange={(e) => setName(e.target.value)} autoFocus />
            </Field>
            <Field label="Description" hint="What this monitor is for, and who relies on it.">
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={3}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
              />
            </Field>
            <Field label="Schedule" hint="When this monitor's checks run automatically (UTC).">
              <SchedulePicker value={scheduleCron} onChange={setScheduleCron} />
              {checksOff && scheduleCron && (
                <p className="mt-2 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
                  The background scheduler is off on this server, so this schedule is saved but
                  won't run until it's enabled (Admin → System, or set
                  <span className="font-mono"> DVP_SCHEDULER_ENABLED=true</span>). Use
                  <span className="font-medium"> Check now</span> to run on demand.
                </p>
              )}
            </Field>
            <Field
              label="Analysis window"
              hint="How far back this monitor analyzes data for baselines, anomaly detection, and charts."
            >
              <select
                value={lookbackDays}
                onChange={(e) => setLookbackDays(Number(e.target.value))}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
              >
                <option value={7}>Last 7 days</option>
                <option value={14}>Last 14 days</option>
                <option value={30}>Last 30 days</option>
                <option value={60}>Last 60 days</option>
                <option value={90}>Last 90 days</option>
                <option value={180}>Last 180 days</option>
                <option value={365}>Last 365 days</option>
              </select>
            </Field>
            <div className="border-t border-slate-100 pt-3">
              <div className="mb-2 text-sm font-semibold text-navy">Alerts</div>
              <p className="mb-3 text-xs text-slate-500">
                Severe and warning thresholds — applied to every table and file in this monitor.
              </p>
              <AlertPolicyFields
                value={alertPolicy}
                onChange={setAlertPolicy}
                isFile={alertPolicy.freshness.source === "file_arrival"}
              />
            </div>
            {formErr && <p className="text-sm text-rose-600">{formErr}</p>}
            <div className="flex gap-2">
              <Button onClick={saveDetails} disabled={busy || !name.trim()}>
                Save
              </Button>
              <Button variant="ghost" onClick={() => setEditing(false)} disabled={busy}>
                Cancel
              </Button>
            </div>
          </Card>
        )}
        {removing && (
          <Card className="mt-4 max-w-xl space-y-3 p-4">
            <p className="text-sm text-slate-700">
              Remove <span className="font-semibold text-navy">{monitor.name}</span> from the list.
              Scan history stays, and this action is recorded with your name.
            </p>
            <Field label="Type the monitor name to confirm">
              <TextInput
                value={confirmName}
                onChange={(e) => setConfirmName(e.target.value)}
                placeholder={monitor.name}
                autoFocus
              />
            </Field>
            {formErr && <p className="text-sm text-rose-600">{formErr}</p>}
            <div className="flex gap-2">
              <Button
                variant="danger"
                onClick={removeMonitor}
                disabled={busy || confirmName !== monitor.name}
              >
                Remove monitor
              </Button>
              <Button variant="ghost" onClick={() => setRemoving(false)} disabled={busy}>
                Cancel
              </Button>
            </div>
          </Card>
        )}
        <p className="mt-1 text-sm text-slate-500">
          {scopeLabel(monitor)}
          {monitor.watermark_column && (
            <>
              {" "}
              · watermark <span className="font-medium text-slate-600">{monitor.watermark_column}</span>
            </>
          )}
          {scoped > tables.length && (
            <>
              {" "}
              · {tables.length} of {scoped} profiled
            </>
          )}
        </p>
      </div>

      <Tabs
        tabs={[
          { key: "overview", label: "Overview" },
          { key: "checks", label: "Checks", count: tables.length },
          { key: "activity", label: "Activity" },
          { key: "settings", label: "Settings" },
        ]}
        active={tab}
        onChange={(k) => setTab(k as typeof tab)}
      />

      {tab === "overview" && (
        <div className="space-y-6">
          {openInc && (
            <div className="flex items-center justify-between gap-3 rounded-xl border border-amber-200 bg-amber-50 px-5 py-4">
              <div className="min-w-0">
                <div className="text-sm font-semibold text-amber-900">⚠ {openInc.summary ?? "Open incident"}</div>
                <div className="mt-0.5 text-xs text-amber-700">
                  {openInc.status} · {String(openInc.severity)}
                  {openInc.dimension ? ` · ${openInc.dimension}` : ""} · detected {fmtWhen(openInc.created_at)}
                </div>
              </div>
              <Link to={incidentHref(monitorId, openInc)} className="shrink-0 text-sm font-medium text-amber-900 hover:underline">
                Investigate incident →
              </Link>
            </div>
          )}

          <div className="grid gap-4 lg:grid-cols-3">
            <Card className="p-5 lg:col-span-2">
              <div className="mb-1 flex items-center justify-between">
                <h2 className="text-sm font-semibold text-navy">Volume{repRef ? ` · ${repRef.split(".").slice(-1)[0]}` : ""}</h2>
                <span className="text-xs text-slate-400">last {monitor.lookback_days ?? 30} days</span>
              </div>
              <TrendChart points={volumeHist} band color="#00a8c8" height={200} />
            </Card>
            {tables && <MonitorHealthCard tables={tables} />}
          </div>

      <div className="flex flex-wrap gap-2">
        <HealthBadge status="critical" label={`${counts.broken} broken`} />
        <HealthBadge status="unknown" label={`${counts.setup} setting up`} />
        <HealthBadge status="healthy" label={`${counts.healthy} healthy`} />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Find a table…"
          className="w-64 rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal"
        />
        {FILTERS.map((f) => (
          <button
            key={f.id}
            onClick={() => setFilter(f.id)}
            className={`rounded-full px-3 py-1 text-xs font-medium ${
              filter === f.id ? "bg-navy text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"
            }`}
          >
            {f.label}
            {f.id !== "all" && tables.length > 0 ? ` ${counts[f.id]}` : ""}
          </button>
        ))}
        <span className="text-xs text-slate-400">
          {shown.length === tables.length
            ? `${tables.length} table${tables.length === 1 ? "" : "s"}`
            : `${shown.length} of ${tables.length}`}
        </span>
      </div>

      {tables.length === 0 ? (
        <EmptyState
          title="No tables profiled yet"
          hint="This monitor has no scan results. Profile it from create, or open it after the next scheduled run."
        />
      ) : shown.length === 0 ? (
        <EmptyState title="No tables match" hint="Try a different name or health filter." />
      ) : (
        <Card className="overflow-hidden">
          <table className="w-full text-sm">
            <thead className="border-b border-slate-100 text-left text-xs uppercase text-slate-400">
              <tr>
                <SortTh label="Table" columnKey="table" sort={sort} onSort={requestSort} className="px-5 py-2 font-medium" />
                <SortTh label="Overall" columnKey="overall" sort={sort} onSort={requestSort} className="px-5 py-2 font-medium" />
                {PILLARS.map((p) => (
                  <SortTh key={p} label={p} columnKey={p} sort={sort} onSort={requestSort} className="px-3 py-2 font-medium capitalize" />
                ))}
                <SortTh label="Last scanned" columnKey="last_scanned" sort={sort} onSort={requestSort} className="px-3 py-2 font-medium" />
                <SortTh label="Scans" columnKey="scans" sort={sort} onSort={requestSort} className="px-3 py-2 font-medium" />
              </tr>
            </thead>
            <tbody>
              {sortedTables.map((t) => {
                const standing = tableStanding(t);
                return (
                  <tr key={t.table_ref} className="border-b border-slate-50 hover:bg-slate-50/60 last:border-0">
                    <td className="px-5 py-3">
                      <Link
                        to={`/observability/${monitor.id}/tables/${encodeURIComponent(t.table_ref)}`}
                        className="font-medium text-teal-700 hover:underline"
                      >
                        {t.table_ref.split(".").slice(-1)[0]}
                      </Link>
                      <div className="text-xs text-slate-400">{t.table_ref}</div>
                    </td>
                    <td className="px-5 py-3">
                      <HealthBadge
                        status={countTone(standing)}
                        label={
                          honestyBadge(
                            t.health,
                            t.score,
                            PILLARS.map((p) => t.pillars[p]),
                          ).label
                        }
                      />
                    </td>
                    {PILLARS.map((p) => (
                      <td key={p} className="px-3 py-3">
                        <HealthBadge status={(t.pillars[p] as Health) ?? "unknown"} />
                        {p === "volume" && t.volume_note && (
                          <div className="mt-1 max-w-40 text-[11px] leading-snug text-slate-500">{t.volume_note}</div>
                        )}
                        {p === "freshness" && t.freshness_note && (
                          <div className="mt-1 max-w-40 text-[11px] leading-snug text-slate-500">{t.freshness_note}</div>
                        )}
                        {p === "schema" && t.schema_note && (
                          <div className="mt-1 max-w-40 text-[11px] leading-snug text-slate-500">{t.schema_note}</div>
                        )}
                        {p === "quality" && t.quality_note && (
                          <div className="mt-1 max-w-40 text-[11px] leading-snug text-slate-500">{t.quality_note}</div>
                        )}
                      </td>
                    ))}
                    <td className="px-3 py-3 text-xs text-slate-500">{fmtWhen(t.last_scanned)}</td>
                    <td className="px-3 py-3 text-xs font-medium text-slate-600">{t.scan_count ?? 0}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>
      )}
        </div>
      )}

      {tab === "checks" && (
        <div className="space-y-6">
      {/* Data quality rules — part of this monitor; add/manage per table without leaving it. */}
      {tables && tables.length > 0 && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-base font-semibold text-navy">Data quality rules</h2>
              <p className="mt-0.5 text-xs text-slate-400">
                Rules belong to this monitor. Choose a table to add or manage its checks.
              </p>
            </div>
            <div className="w-64">
              <Combobox
                value={rulesTable || tables[0].table_ref}
                onChange={setRulesTable}
                allowCustom={false}
                options={tables.map((t) => ({
                  value: t.table_ref,
                  label: t.table_ref.split(".").slice(-1)[0],
                }))}
              />
            </div>
          </div>
          <ActiveTablePattern
            monitorId={monitorId}
            tableRef={rulesTable || tables[0].table_ref}
            onPattern={setRulesTablePattern}
          />
          {monitor && rulesTablePattern && rulesTablePattern !== "unknown" && (
            <SuggestedRules
              monitorId={monitorId}
              connectionId={monitor.connection_id}
              tableRef={rulesTable || tables[0].table_ref}
              loadPattern={rulesTablePattern}
              onAdded={() => setMetricsRefresh((n) => n + 1)}
            />
          )}
          <RulesPanel monitorId={monitorId} tableRef={rulesTable || tables[0].table_ref} />
        </div>
      )}

      {/* Baseline metrics (history-relative DQ) — populated once baseline rules are added. */}
      {tables && tables.length > 0 && (
        <MetricsPanel monitorId={monitorId} refreshSignal={metricsRefresh} />
      )}
        </div>
      )}

      {tab === "activity" && (
      <Card className="px-5 py-4">
        <h2 className="text-sm font-semibold text-navy">Activity</h2>
        <p className="mt-0.5 text-xs text-slate-400">Who created, changed, or removed this monitor.</p>
        {audit === null ? (
          <div className="mt-3">
            <Spinner label="Loading activity…" />
          </div>
        ) : audit.length === 0 ? (
          <p className="mt-3 text-sm text-slate-400">No recorded actions yet.</p>
        ) : (
          <ul className="mt-3 divide-y divide-slate-100">
            {audit.map((entry) => (
              <li key={entry.id} className="py-2">
                {auditSentences(entry).map((line, i) => (
                  <div key={i} className="text-sm text-slate-700">
                    {line}
                  </div>
                ))}
                <div className="text-xs text-slate-400">
                  {entry.actor || entry.actor_email || "Unknown user"}
                  {entry.actor_email && entry.actor && entry.actor !== entry.actor_email
                    ? ` · ${entry.actor_email}`
                    : ""}
                  {" · "}
                  {fmtWhen(entry.ts)}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
      )}

      {tab === "settings" && (
        <div className="max-w-xl space-y-4">
          <Card className="space-y-3 p-5">
            <h2 className="text-sm font-semibold text-navy">Monitor settings</h2>
            <p className="text-sm text-slate-500">Edit thresholds, schedule, and ownership in the guided flow.</p>
            <div>
              <Button onClick={() => navigate(`/observability/${monitor.id}/edit`)}>Edit monitor</Button>
            </div>
          </Card>
          <Card className="space-y-3 p-5">
            <h2 className="text-sm font-semibold text-navy">Danger zone</h2>
            <p className="text-sm text-slate-500">Remove this monitor from the list. Scan history is kept.</p>
            <div>
              <Button
                variant="ghost"
                className="text-rose-700 ring-rose-200 hover:bg-rose-50"
                onClick={() => { setConfirmName(""); setFormErr(null); setRemoving(true); }}
              >
                Remove monitor
              </Button>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}
