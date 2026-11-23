import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import type { Connection, Incident, Monitor, MonitorSummary, TableSummary } from "../types";
import { Button, Card, Chip, Icon, KpiCard, PageTitle, Pill, Tabs, type StatusTone } from "../components/vex";
import { PILLARS, annotateTables, fmtWhen, tableStanding } from "./MonitorDetail";

const SOURCE_LABEL: Record<string, string> = {
  snowflake: "Snowflake",
  s3: "Amazon S3",
  file: "Files",
  oracle: "Oracle",
  bigquery: "BigQuery",
  postgres: "PostgreSQL",
  postgresql: "PostgreSQL",
  inline: "Local",
  local: "Local",
  local_sql: "Local",
  sqlite: "Local",
};
const sourceLabel = (t?: string) => (t ? SOURCE_LABEL[t] ?? t[0].toUpperCase() + t.slice(1) : "—");

// Per-monitor health derived from its tables (the dashboard STATUS column).
type Derived = {
  tone: StatusTone;
  label: string;
  assets: number;
  passing: number;
  total: number;
  lastRun: string | null;
};

function deriveHealth(monitor: Monitor, tables: TableSummary[] | undefined): Derived {
  if (monitor.status && monitor.status !== "active") {
    return { tone: "paused", label: "Paused", assets: tables?.length ?? 0, passing: 0, total: 0, lastRun: monitor.last_run_at ?? null };
  }
  const t = tables ?? [];
  let broken = false;
  let setup = t.length === 0;
  let passing = 0;
  for (const table of t) {
    const s = tableStanding(table);
    if (s === "broken") broken = true;
    if (s === "setup") setup = true;
    passing += PILLARS.filter((p) => table.pillars[p] === "healthy").length;
  }
  const total = t.length * PILLARS.length;
  const lastRun = t.map((x) => x.last_scanned).filter(Boolean).sort().slice(-1)[0] ?? monitor.last_run_at ?? null;
  if (broken) return { tone: "attention", label: "Needs attention", assets: t.length, passing, total, lastRun };
  if (setup) return { tone: "learning", label: "Learning", assets: t.length, passing, total, lastRun };
  return { tone: "healthy", label: "Healthy", assets: t.length, passing, total, lastRun };
}

type Filter = "all" | StatusTone;

export default function Monitors() {
  const nav = useNavigate();
  const [summary, setSummary] = useState<MonitorSummary | null>(null);
  const [monitors, setMonitors] = useState<Monitor[] | null>(null);
  const [tablesBy, setTablesBy] = useState<Record<string, TableSummary[]>>({});
  const [ready, setReady] = useState(false);
  const [conns, setConns] = useState<Record<string, Connection>>({});
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [filter, setFilter] = useState<Filter>("all");
  const [q, setQ] = useState("");

  const load = () => {
    setReady(false);
    api.monitorsSummary().then((r) => setSummary(r.data)).catch(() => setSummary(null));
    api.listConnections().then((r) => setConns(Object.fromEntries(r.data.map((c) => [c.id, c])))).catch(() => undefined);
    api.listIncidents(true).then((r) => setIncidents(r.data)).catch(() => setIncidents([]));
    api.listMonitors().then(async (r) => {
      setMonitors(r.data);
      const entries = await Promise.all(
        r.data.map(async (m) => {
          try {
            const t = await api.listTables(m.id);
            return [m.id, await annotateTables(m.id, t.data)] as const;
          } catch {
            return [m.id, [] as TableSummary[]] as const;
          }
        }),
      );
      setTablesBy(Object.fromEntries(entries));
      setReady(true);
    }).catch(() => setMonitors([]));
  };
  useEffect(load, []);

  const openByMonitor = useMemo(() => {
    const m: Record<string, number> = {};
    for (const i of incidents) if (i.monitor_id && i.status !== "resolved") m[i.monitor_id] = (m[i.monitor_id] ?? 0) + 1;
    return m;
  }, [incidents]);

  const rows = useMemo(() => {
    return (monitors ?? []).map((m) => {
      const d = deriveHealth(m, ready ? tablesBy[m.id] ?? [] : undefined);
      const conn = conns[m.connection_id];
      return {
        m,
        d,
        source: sourceLabel(conn?.source_type),
        schema: m.scope?.schema ?? (m.scope?.database ?? "—"),
        open: openByMonitor[m.id] ?? 0,
        owner: m.source_system || m.layer || "—",
      };
    });
  }, [monitors, tablesBy, conns, openByMonitor, ready]);

  const chipCounts = useMemo(() => {
    const c: Record<string, number> = { all: rows.length, healthy: 0, attention: 0, learning: 0, paused: 0 };
    for (const r of rows) c[r.d.tone] = (c[r.d.tone] ?? 0) + 1;
    return c;
  }, [rows]);

  const shown = rows.filter((r) => {
    if (filter !== "all" && r.d.tone !== filter) return false;
    if (q.trim()) {
      const s = q.trim().toLowerCase();
      return [r.m.name, r.source, r.schema, r.owner].some((v) => (v || "").toLowerCase().includes(s));
    }
    return true;
  });

  // Column sort. Click a header to sort; click again to flip direction.
  type SortKey = "name" | "status" | "source" | "schema" | "assets" | "passing" | "incidents" | "lastrun" | "created" | "owner";
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "name", dir: 1 });
  const toggleSort = (key: SortKey) =>
    setSort((s) => (s.key === key ? { key, dir: s.dir === 1 ? -1 : 1 } : { key, dir: 1 }));
  const sortVal = (r: (typeof shown)[number], key: SortKey): string | number => {
    switch (key) {
      case "name": return r.m.name.toLowerCase();
      case "status": return r.d.label.toLowerCase();
      case "source": return r.source.toLowerCase();
      case "schema": return (r.schema || "").toLowerCase();
      case "assets": return r.d.assets;
      case "passing": return r.d.total ? r.d.passing / r.d.total : -1;
      case "incidents": return r.open;
      case "lastrun": return r.d.lastRun || "";
      case "created": return r.m.created_at || "";
      case "owner": return (r.owner || "").toLowerCase();
    }
  };
  const sorted = useMemo(() => {
    const out = [...shown];
    out.sort((a, b) => {
      const va = sortVal(a, sort.key);
      const vb = sortVal(b, sort.key);
      return (va < vb ? -1 : va > vb ? 1 : 0) * sort.dir;
    });
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shown, sort]);

  const kHealthy = summary?.health.healthy ?? chipCounts.healthy;
  const kAttention = summary ? summary.health.warning + summary.health.error : chipCounts.attention;
  const kPaused = summary?.health.paused ?? chipCounts.paused;
  const kTotal = summary?.total ?? rows.length;

  return (
    <div className="space-y-6">
      <PageTitle
        title="Data Observability"
        subtitle="Monitor your data, spot unexpected changes, and investigate issues in one place."
        actions={
          <Button onClick={() => nav("/observability/new")}>
            <Icon name="plus" className="h-4 w-4" /> Create monitor
          </Button>
        }
      />

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <KpiCard label="Total monitors" value={kTotal} hint={`${rows.reduce((a, r) => a + r.d.assets, 0)} tables & file datasets`} />
        <KpiCard label="Healthy" value={kHealthy} hint="All configured checks passing" tone="healthy" />
        <KpiCard label="Needs attention" value={kAttention} hint={`${summary?.incidents.active_total ?? 0} open incidents`} tone="attention" />
        <KpiCard label="Learning / paused" value={`${chipCounts.learning} / ${kPaused}`} hint="Baselines building · checks paused" tone="learning" />
      </div>

      <Tabs
        tabs={[{ key: "monitors", label: "Monitors" }, { key: "incidents", label: "Incidents", count: summary?.incidents.active_total }]}
        active="monitors"
        onChange={(k) => k === "incidents" && nav("/incidents")}
      />

      <div className="flex flex-wrap items-center gap-2">
        <Chip label="All monitors" count={chipCounts.all} active={filter === "all"} onClick={() => setFilter("all")} />
        <Chip label="Healthy" count={chipCounts.healthy} tone="healthy" active={filter === "healthy"} onClick={() => setFilter("healthy")} />
        <Chip label="Needs attention" count={chipCounts.attention} tone="attention" active={filter === "attention"} onClick={() => setFilter("attention")} />
        <Chip label="Learning" count={chipCounts.learning} tone="learning" active={filter === "learning"} onClick={() => setFilter("learning")} />
        <Chip label="Paused" count={chipCounts.paused} tone="paused" active={filter === "paused"} onClick={() => setFilter("paused")} />
        <div className="ml-auto flex items-center gap-2 text-xs text-slate-400">
          <span>Updated {summary ? "just now" : "…"}</span>
          <Button variant="ghost" size="sm" onClick={load}>
            <Icon name="refresh" className="h-3.5 w-3.5" /> Refresh
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[220px]">
          <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400">
            <Icon name="search" className="h-4 w-4" />
          </span>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search monitors or schemas"
            className="w-full rounded-lg border border-slate-300 bg-white py-2 pl-9 pr-3 text-sm outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
          />
        </div>
      </div>

      <Card className="overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-200 bg-slate-50/60 text-left text-[11px] uppercase tracking-wide text-slate-400">
              {([
                ["name", "Monitor", ""],
                ["status", "Status", ""],
                ["source", "Source", ""],
                ["schema", "Schema", ""],
                ["assets", "Assets", "text-right"],
                ["passing", "Passing", "text-right"],
                ["incidents", "Incidents", ""],
                ["lastrun", "Last run", ""],
                ["created", "Created", ""],
                ["owner", "Owner", ""],
              ] as [SortKey, string, string][]).map(([key, label, align]) => (
                <th key={key} className={`px-4 py-2.5 font-semibold ${align}`}>
                  <button
                    onClick={() => toggleSort(key)}
                    className={`inline-flex items-center gap-1 uppercase tracking-wide hover:text-slate-600 ${sort.key === key ? "text-slate-600" : ""}`}
                  >
                    {label}
                    <span className="text-[9px]">{sort.key === key ? (sort.dir === 1 ? "▲" : "▼") : "↕"}</span>
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {monitors === null ? (
              <tr><td colSpan={10} className="px-4 py-10 text-center text-slate-400">Loading…</td></tr>
            ) : sorted.length === 0 ? (
              <tr><td colSpan={10} className="px-4 py-10 text-center text-slate-400">No monitors match.</td></tr>
            ) : (
              sorted.map(({ m, d, source, schema, open, owner }) => (
                <tr
                  key={m.id}
                  onClick={() => nav(`/observability/${m.id}`)}
                  className="cursor-pointer border-b border-slate-100 last:border-0 hover:bg-slate-50/70"
                >
                  <td className="px-4 py-3">
                    <div className="font-semibold text-navy">{m.name}</div>
                    <div className="text-xs text-slate-400">{d.assets} {d.assets === 1 ? "asset" : "assets"}</div>
                  </td>
                  <td className="px-4 py-3"><Pill tone={d.tone}>{d.label}</Pill></td>
                  <td className="px-4 py-3 text-slate-600">{source}</td>
                  <td className="px-4 py-3 text-slate-600">{schema}</td>
                  <td className="px-4 py-3 text-right tabular-nums text-slate-600">{d.assets}</td>
                  <td className="px-4 py-3 text-right tabular-nums">
                    {d.total > 0 ? (
                      <span className={d.passing < d.total ? "text-amber-600" : "text-slate-600"}>{d.passing}/{d.total}</span>
                    ) : (
                      <span className="text-slate-300">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    {open > 0 ? (
                      <span className="text-rose-600">{open} open</span>
                    ) : (
                      <span className="text-slate-300">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-slate-500">{fmtWhen(d.lastRun)}</td>
                  <td className="px-4 py-3 text-slate-500">{fmtWhen(m.created_at ?? null)}</td>
                  <td className="px-4 py-3 text-slate-500">{owner}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </Card>
      <p className="text-center text-xs text-slate-400">
        Showing {shown.length} of {rows.length} monitors · each monitor is scoped to one schema.
      </p>
    </div>
  );
}
