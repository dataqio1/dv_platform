import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import type { ColumnStat, HistoryPoint, Incident, PillarCard, TablePillars } from "../types";
import { Button, Card, HealthBadge, honestyBadge, Spinner } from "../components/ui";
import { TrendChart } from "../components/TrendChart";
import { RulesPanel } from "../components/RulesPanel";
import { IncidentList } from "../components/IncidentList";
import { TYPE_GAP, qualityGapSentence, suggestWatermark, volumeGapSentence } from "./MonitorDetail";

const PATTERN_LABEL: Record<string, string> = {
  append: "append-only",
  cdc: "upsert / CDC",
  full_snapshot: "full snapshot",
  scd2: "SCD type 2",
  partitioned: "partitioned",
  reference: "reference",
  unknown: "unknown",
};
const PATTERN_COLOR: Record<string, string> = {
  append: "bg-sky-100 text-sky-700",
  cdc: "bg-teal/10 text-teal-700",
  full_snapshot: "bg-amber-100 text-amber-700",
  scd2: "bg-violet-100 text-violet-700",
  partitioned: "bg-indigo-100 text-indigo-700",
  reference: "bg-slate-200 text-slate-600",
  unknown: "bg-slate-100 text-slate-500",
};

function LoadPatternControl({
  tableRef,
  pattern,
  source,
  onChange,
}: {
  tableRef: string;
  pattern: string;
  source?: string;
  onChange: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const injected = source && !["user", "auto", "monitor_default"].includes(source);
  const set = async (p: string) => {
    setEditing(false);
    await api.setTableMetadata(tableRef, { load_pattern: p }).catch(() => undefined);
    onChange();
  };
  if (editing)
    return (
      <select
        autoFocus
        defaultValue={pattern}
        onBlur={() => setEditing(false)}
        onChange={(e) => set(e.target.value)}
        className="rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[11px]"
      >
        {Object.keys(PATTERN_LABEL).map((p) => (
          <option key={p} value={p}>
            {PATTERN_LABEL[p]}
          </option>
        ))}
      </select>
    );
  return (
    <button
      onClick={() => setEditing(true)}
      title={injected ? `load pattern injected by ${source}` : "load pattern (click to change)"}
      className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${PATTERN_COLOR[pattern] ?? PATTERN_COLOR.unknown}`}
    >
      {PATTERN_LABEL[pattern] ?? pattern}
      {injected && <span className="ml-1 opacity-60">· via {source}</span>}
    </button>
  );
}

const PILLAR_LABEL: Record<string, string> = {
  freshness: "Freshness",
  volume: "Volume",
  schema: "Schema",
  quality: "Quality/Distribution",
};

const PILLAR_COLOR: Record<string, string> = {
  freshness: "#17a2b8",
  volume: "#6366f1",
  schema: "#0ea5e9",
  quality: "#f59e0b",
};

// Per-column attention markers — one color per signal kind so issues are scannable at a glance.
const ATTENTION_STYLE: Record<string, { color: string; label: string }> = {
  null_spike: { color: "#f59e0b", label: "null spike" },
  cardinality_drift: { color: "#8b5cf6", label: "drift" },
  type_change: { color: "#3b82f6", label: "type change" },
  all_null: { color: "#ef4444", label: "all null" },
};
function attentionStyle(kind: string | null | undefined): { color: string; label: string } {
  return ATTENTION_STYLE[kind ?? ""] ?? { color: "#f59e0b", label: "attention" };
}

function LineageSide({
  title,
  rows,
}: {
  title: string;
  rows?: { name: string; hops: number }[];
}) {
  if (!rows || rows.length === 0) return null;
  return (
    <div className="mt-3">
      <div className="text-xs font-medium uppercase tracking-wide text-slate-400">{title}</div>
      <ul className="mt-1 space-y-1">
        {rows.map((row) => (
          <li key={`${title}-${row.name}`} className="text-sm text-navy">
            {row.name.split(".").slice(-1)[0]}
            <span className="ml-2 text-xs text-slate-400">
              {row.hops} hop{row.hops === 1 ? "" : "s"}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function offeredFreshness(
  pillars: PillarCard[],
  suggestion: { column: string } | null,
  watermark: string | null | undefined,
): PillarCard[] {
  if (watermark || !suggestion) return pillars;
  return pillars.map((card) => {
    if (card.pillar !== "freshness" || card.headline !== "Not configured yet") return card;
    return {
      ...card,
      status: "unknown",
      headline: `${suggestion.column} looks like the load time`,
      detail: "Confirm it and the next check reads that column",
      configured: true,
    };
  });
}

function thinVolume(pillars: PillarCard[], points: HistoryPoint[] | undefined): PillarCard[] {
  if (!points || points.length === 0) return pillars;
  return pillars.map((card) => {
    if (card.pillar !== "volume" || card.detail !== "not enough history to alert") return card;
    const detail = volumeGapSentence(points);
    return detail ? { ...card, detail } : card;
  });
}

function unscoredQuality(pillars: PillarCard[], queryShape: string | null | undefined): PillarCard[] {
  const headline = qualityGapSentence(pillars, queryShape);
  if (!headline) return pillars;
  return pillars.map((card) => {
    if (card.pillar !== "quality" || card.headline !== "Not configured yet") return card;
    return { ...card, status: "unknown", headline, detail: null, configured: true };
  });
}

function unreadWatermark(pillars: PillarCard[], watermark: string | null | undefined): PillarCard[] {
  if (!watermark) return pillars;
  const headline = `No scan has read ${watermark}`;
  return pillars.map((card) => {
    if (card.pillar !== "freshness" || card.headline !== "Not configured yet") return card;
    return { ...card, status: "unknown", headline, detail: null, configured: true };
  });
}

function schemaWithoutTypes(pillars: PillarCard[], columns: ColumnStat[] | null): PillarCard[] {
  if (!columns || columns.length === 0 || columns.some((column) => column.data_type)) return pillars;
  return pillars.map((card) => {
    if (card.pillar !== "schema" || card.detail === TYPE_GAP) return card;
    if (card.status === "warning" || card.status === "critical") return card;
    return { ...card, status: "unknown", detail: TYPE_GAP, configured: true };
  });
}

function PillarCardView({
  card,
  active,
  onClick,
}: {
  card: PillarCard;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`block w-full rounded-xl border bg-white p-5 text-left shadow-sm transition hover:shadow ${
        active ? "border-teal ring-2 ring-teal/20" : "border-slate-200"
      }`}
    >
      <div className="flex items-start justify-between">
        <span className="text-sm font-semibold text-slate-500">{PILLAR_LABEL[card.pillar]}</span>
        <HealthBadge status={card.status} />
      </div>
      <div className="mt-3 text-lg font-bold text-navy">{card.headline}</div>
      {card.detail && <div className="mt-0.5 text-xs text-slate-400">{card.detail}</div>}
    </button>
  );
}

export default function TableObservability() {
  const { monitorId = "", tableRef = "" } = useParams();
  const [data, setData] = useState<TablePillars | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [histories, setHistories] = useState<Record<string, HistoryPoint[]>>({});
  const [focus, setFocus] = useState<string | null>(null);
  const [columns, setColumns] = useState<ColumnStat[] | null>(null);
  const [activeCol, setActiveCol] = useState<string | null>(null);
  const [colHistory, setColHistory] = useState<Record<string, HistoryPoint[]> | null>(null);
  const [profiling, setProfiling] = useState(false);
  const [savingWatermark, setSavingWatermark] = useState(false);
  const [downstreamName, setDownstreamName] = useState("");
  const [savingLineage, setSavingLineage] = useState(false);
  const [lineageErr, setLineageErr] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [confirmProfile, setConfirmProfile] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  // null = use the monitor's configured analysis window (lookback); 0 = all history; N = N days.
  const [windowDays, setWindowDays] = useState<number | null>(null);
  const [lookback, setLookback] = useState(30);
  const [monitorName, setMonitorName] = useState<string | null>(null);
  // The window actually charted: the monitor's lookback unless the user picked one.
  const effectiveDays = windowDays === null ? lookback : windowDays;

  useEffect(() => {
    api
      .getMonitor(monitorId)
      .then((r) => {
        setMonitorName(r.data.name);
        setLookback(r.data.lookback_days ?? 30);
      })
      .catch(() => setMonitorName(null));
  }, [monitorId]);

  const loadIncidents = useCallback(() => {
    api
      .tableIncidents(monitorId, tableRef)
      .then((r) => setIncidents(r.data))
      .catch(() => setIncidents([]));
  }, [monitorId, tableRef]);

  const loadColumnHistory = useCallback(
    (col: string) => {
      setActiveCol(col);
      setColHistory(null);
      api.columnHistory(monitorId, tableRef, col).then((r) => setColHistory(r.data.metrics));
    },
    [monitorId, tableRef]
  );

  const load = useCallback(() => {
    api
      .tablePillars(monitorId, tableRef)
      .then((r) => {
        setData(r.data);
        // Load history for every configured pillar up front → charts show by default.
        const configured = r.data.pillars.filter((p) => p.configured);
        if (configured.length && !focus) setFocus(configured[0].pillar);
        Promise.all(
          configured.map((p) =>
            api
              .pillarHistory(monitorId, tableRef, p.pillar, effectiveDays)
              .then((h) => [p.pillar, h.data.points] as const)
              .catch(() => [p.pillar, [] as HistoryPoint[]] as const)
          )
        ).then((pairs) => setHistories(Object.fromEntries(pairs)));
      })
      .catch((e) => setErr(e.message));
    api.tableColumns(monitorId, tableRef).then((r) => {
      setColumns(r.data);
      if (r.data.length) loadColumnHistory(r.data[0].column); // auto-select first column
    });
    loadIncidents();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [monitorId, tableRef, effectiveDays]);

  const ackIncident = (id: string) => api.ackIncident(id).then(loadIncidents);
  const resolveIncident = (id: string) => api.resolveIncident(id).then(loadIncidents);

  useEffect(() => {
    load();
  }, [load]);

  const profileNow = async () => {
    setProfiling(true);
    try {
      await api.profileTable(monitorId, tableRef);
      load();
    } finally {
      setProfiling(false);
    }
  };

  const runNow = async () => {
    setRunning(true);
    try {
      await api.runMonitor(monitorId); // pushdown: profile + evaluate rules
      load();
      setRefreshKey((k) => k + 1); // refresh the rules panel too
    } finally {
      setRunning(false);
    }
  };

  if (err) return <p className="text-sm text-rose-600">{err}</p>;
  if (!data) return <Spinner />;

  const watermarkOffer = data.watermark_column
    ? null
    : (data.suggested_watermark ?? suggestWatermark(columns));
  const pillars = offeredFreshness(
    thinVolume(
      unscoredQuality(
        unreadWatermark(schemaWithoutTypes(data.pillars, columns), data.watermark_column),
        data.query_shape,
      ),
      histories.volume,
    ),
    watermarkOffer,
    data.watermark_column,
  );
  const configured = pillars.filter((p) => p.configured);
  const focusCard = pillars.find((p) => p.pillar === focus) ?? configured[0];

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <div className="text-xs text-slate-400">
            <Link to="/observability" className="hover:text-navy">Data Observability</Link>
            {" / "}
            <Link to={`/observability/${monitorId}`} className="hover:text-navy">{monitorName ?? "Monitor"}</Link>
          </div>
          <div className="mt-1 flex items-center gap-3">
            <h1 className="text-2xl font-bold text-navy">{data.table_ref}</h1>
            <HealthBadge
              {...honestyBadge(
                data.health,
                data.score,
                pillars.map((p) => p.status),
              )}
            />
          </div>
          <div className="mt-1 flex items-center gap-2 text-xs text-slate-400">
            <span>
              {data.last_profiled_at
                ? `Last profiled ${new Date(data.last_profiled_at).toLocaleString()}`
                : "Not profiled yet"}
              {data.query_shape === "check"
                ? " · last check read the watermark and a row count"
                : data.query_shape === "full_profile"
                  ? " · last run was a full column profile"
                  : ""}
              {data.watermark_column && (
                <> · watermark: <b className="text-slate-500">{data.watermark_column}</b></>
              )}
            </span>
            <LoadPatternControl
              tableRef={data.table_ref}
              pattern={data.load_pattern || "unknown"}
              source={data.load_pattern_source}
              onChange={load}
            />
          </div>
        </div>
        <div className="flex gap-2">
          <Button variant="ghost" onClick={() => setConfirmProfile(true)} disabled={profiling}>
            Full profile
          </Button>
          <Button onClick={runNow} disabled={running}>
            {running ? "Checking…" : "Check now"}
          </Button>
        </div>
      </div>
      {confirmProfile && (
        <Card className="max-w-xl space-y-3 p-4">
          <p className="text-sm font-medium text-navy">
            Full profile scans every column of {data.table_ref}.
          </p>
          <div className="flex gap-2">
            <Button
              onClick={async () => {
                await profileNow();
                setConfirmProfile(false);
              }}
              disabled={profiling}
            >
              {profiling ? "Scanning…" : "Scan this table"}
            </Button>
            <Button variant="ghost" onClick={() => setConfirmProfile(false)} disabled={profiling}>
              Cancel
            </Button>
          </div>
        </Card>
      )}

      {/* Four pillars + lineage */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {pillars.map((c) => (
          <PillarCardView
            key={c.pillar}
            card={c}
            active={focus === c.pillar}
            onClick={() => c.configured && setFocus(c.pillar)}
          />
        ))}
        <Card className="p-5">
          <div className="flex items-start justify-between">
            <span className="text-sm font-semibold text-slate-500">Lineage</span>
            <HealthBadge
              status={data.lineage.status === "mapped" ? "healthy" : "unknown"}
              label={data.lineage.status}
            />
          </div>
          <LineageSide title="Upstream" rows={data.lineage.upstream} />
          <LineageSide title="Downstream" rows={data.lineage.downstream} />
          {(data.lineage.upstream ?? []).length === 0 && (data.lineage.downstream ?? []).length === 0 && (
            <div className="mt-3 space-y-1">
              {data.lineage.nodes.map((n) => (
                <div key={n.name} className="text-sm text-navy">
                  {n.name} <span className="text-xs text-slate-400">({n.type})</span>
                </div>
              ))}
            </div>
          )}
          <form
            className="mt-3 space-y-2"
            onSubmit={async (event) => {
              event.preventDefault();
              const name = downstreamName.trim();
              if (!name) return;
              setSavingLineage(true);
              setLineageErr(null);
              try {
                await api.addLineageEdges([{ upstream: tableRef, downstream: name }]);
                setDownstreamName("");
                load();
              } catch (e) {
                setLineageErr((e as Error).message);
              } finally {
                setSavingLineage(false);
              }
            }}
          >
            <label className="block text-xs text-slate-500" htmlFor="downstream-model">
              Model built on this table
            </label>
            <input
              id="downstream-model"
              value={downstreamName}
              onChange={(e) => setDownstreamName(e.target.value)}
              placeholder="DATABASE.SCHEMA.MODEL"
              className="w-full rounded-lg border border-slate-300 px-2 py-1.5 text-sm outline-none focus:border-teal"
            />
            <Button type="submit" disabled={savingLineage || !downstreamName.trim()}>
              {savingLineage ? "Saving…" : "Add downstream"}
            </Button>
            {lineageErr && <p className="text-xs text-rose-600">{lineageErr}</p>}
          </form>
        </Card>
      </div>
      {watermarkOffer && (
        <div className="flex items-center gap-3">
          <Button
            disabled={savingWatermark}
            onClick={async () => {
              const pick = watermarkOffer;
              if (!pick) return;
              setSavingWatermark(true);
              try {
                await api.setWatermark(monitorId, pick.column, pick.type);
                load();
              } finally {
                setSavingWatermark(false);
              }
            }}
          >
            {savingWatermark ? "Saving…" : `Use ${watermarkOffer.column}`}
          </Button>
          <span className="text-sm text-slate-500">The next check reads that column.</span>
        </div>
      )}

      {/* Active incidents for this table (deduped, evidence-backed) */}
      {incidents.filter((i) => i.status !== "resolved").length > 0 && (
        <Card className="border-rose-200 p-5">
          <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold text-navy">
            <span className="h-2 w-2 rounded-full bg-rose-500" />
            Active incidents
          </h3>
          <IncidentList
            incidents={incidents.filter((i) => i.status !== "resolved")}
            onAck={ackIncident}
            onResolve={resolveIncident}
            onChange={loadIncidents}
            showTable={false}
          />
        </Card>
      )}

      {/* Focused pillar chart (defaults to first configured pillar) */}
      {focusCard && (
        <Card className="p-5">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-sm font-semibold text-navy">
              {PILLAR_LABEL[focusCard.pillar]} — history
              {focusCard.pillar === "volume" && (
                <span className="ml-2 text-xs font-normal text-slate-400">
                  shaded = expected band · red = unusual vs baseline
                </span>
              )}
            </h3>
            <div className="flex items-center gap-3">
              <span className="text-xs text-slate-400">{focusCard.headline}</span>
              <div className="flex gap-1">
                {(
                  [
                    ["1w", 7],
                    ["1m", 30],
                    ["3m", 90],
                    ["1y", 365],
                    ["All", 0],
                  ] as const
                ).map(([lbl, d]) => (
                  <button
                    key={lbl}
                    onClick={() => setWindowDays(d)}
                    className={`rounded px-2 py-0.5 text-[11px] font-medium ${
                      effectiveDays === d
                        ? "bg-navy text-white"
                        : "bg-slate-100 text-slate-600 hover:bg-slate-200"
                    }`}
                  >
                    {lbl}
                  </button>
                ))}
              </div>
            </div>
          </div>
          <TrendChart
            points={histories[focusCard.pillar] ?? []}
            color={PILLAR_COLOR[focusCard.pillar]}
            height={220}
            band={focusCard.pillar === "volume"}
          />
          {/* Flagged points — e.g. data loaded twice (spike) or not loaded (drop). The user
              can remove a point they consider wrong so it stops skewing the chart + baseline. */}
          {(() => {
            const flagged = (histories[focusCard.pillar] ?? []).filter((p) => p.anomaly && p.id);
            if (flagged.length === 0) return null;
            const remove = (id: string) => api.excludePoint(id, true).then(load).catch(() => undefined);
            return (
              <div className="mt-3 border-t border-slate-100 pt-3">
                <div className="mb-1.5 text-xs font-medium text-slate-500">
                  Unusual points — remove any that are wrong (double load / missed load):
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {flagged.slice(-12).map((p) => (
                    <span key={p.id} className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-[11px] text-amber-800">
                      {new Date(p.ts).toLocaleDateString(undefined, { month: "short", day: "numeric" })}:
                      {" "}{p.value.toLocaleString()}
                      <button
                        onClick={() => remove(p.id!)}
                        title="remove this point from the chart and baseline"
                        className="text-amber-400 hover:text-rose-600"
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              </div>
            );
          })()}
        </Card>
      )}

      {/* Trends — all configured pillars at a glance */}
      {configured.length > 1 && (
        <Card className="p-5">
          <h3 className="mb-3 text-sm font-semibold text-navy">Trends</h3>
          <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
            {configured.map((p) => (
              <button
                key={p.pillar}
                onClick={() => setFocus(p.pillar)}
                className="rounded-lg border border-slate-100 p-3 text-left hover:border-slate-200"
              >
                <div className="mb-1 flex items-center justify-between">
                  <span className="text-xs font-medium text-slate-500">{PILLAR_LABEL[p.pillar]}</span>
                  <HealthBadge status={p.status} />
                </div>
                <TrendChart points={histories[p.pillar] ?? []} color={PILLAR_COLOR[p.pillar]} height={120} />
              </button>
            ))}
          </div>
        </Card>
      )}

      {/* Custom data quality rules + their metrics */}
      <RulesPanel monitorId={monitorId} tableRef={tableRef} refreshSignal={refreshKey} />

      {/* Column drill-down (first column auto-selected) */}
      <Card className="p-5">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-sm font-semibold text-navy">Columns</h3>
          {columns && (
            <div className="flex flex-wrap items-center gap-3">
              {[...new Set(columns.filter((c) => c.attention).map((c) => c.attention_kind ?? ""))].map(
                (kind) => (
                  <span key={kind} className="flex items-center gap-1 text-[11px] text-slate-500">
                    <span
                      className="inline-block h-2 w-2 rounded-full"
                      style={{ backgroundColor: attentionStyle(kind).color }}
                    />
                    {attentionStyle(kind).label}
                  </span>
                ),
              )}
            </div>
          )}
        </div>
        {columns === null ? (
          <Spinner />
        ) : columns.length === 0 ? (
          <div className="text-xs text-slate-400">
            No column metrics yet. Record a snapshot with per-column null rates to populate this.
          </div>
        ) : (
          <div className="grid gap-4 md:grid-cols-[260px,1fr]">
            <div className="space-y-1">
              {[...columns]
                .sort((a, b) => Number(b.attention ?? false) - Number(a.attention ?? false))
                .map((c) => (
                <button
                  key={c.column}
                  onClick={() => loadColumnHistory(c.column)}
                  className={`flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-sm ${
                    activeCol === c.column ? "bg-teal/10 text-teal-700" : "hover:bg-slate-50"
                  }`}
                >
                  <span className="min-w-0">
                    <span className="flex items-center gap-1.5 font-medium">
                      {c.attention && (
                        <span
                          title={c.attention_reason ?? "Needs attention"}
                          aria-label={c.attention_reason ?? "needs attention"}
                          className="inline-block h-2 w-2 shrink-0 rounded-full"
                          style={{ backgroundColor: attentionStyle(c.attention_kind).color }}
                        />
                      )}
                      <span className="truncate">{c.column}</span>
                    </span>
                    <span className="block text-xs text-slate-400">
                      {c.data_type || "type not recorded"}
                    </span>
                  </span>
                  <span className="flex shrink-0 items-center gap-2">
                    {c.attention && (
                      <span
                        className="rounded px-1.5 py-0.5 text-[10px] font-medium"
                        title={c.attention_reason ?? undefined}
                        style={{
                          backgroundColor: attentionStyle(c.attention_kind).color + "22",
                          color: attentionStyle(c.attention_kind).color,
                        }}
                      >
                        {attentionStyle(c.attention_kind).label}
                      </span>
                    )}
                    <span className="text-xs text-slate-400">
                      {c.null_rate != null ? `null ${(c.null_rate * 100).toFixed(1)}%` : ""}
                    </span>
                  </span>
                </button>
              ))}
            </div>
            <div>
              {!activeCol || colHistory === null ? (
                <Spinner />
              ) : (
                <div className="space-y-4">
                  <div>
                    <div className="mb-1 text-xs font-medium text-slate-500">{activeCol} — null rate</div>
                    <TrendChart points={colHistory["quality.null_rate"] ?? []} color="#f59e0b" />
                  </div>
                  {(colHistory["quality.distinct_ratio"] ?? []).length > 0 && (
                    <div>
                      <div className="mb-1 flex items-center gap-1.5 text-xs font-medium text-slate-500">
                        {activeCol} — distinct ratio
                        {columns?.find((c) => c.column === activeCol)?.distinct_approx && (
                          <span
                            className="rounded bg-slate-100 px-1 py-0.5 text-[10px] font-normal text-slate-500"
                            title="Distinct counts use the warehouse's approximate (HyperLogLog) function to save cost."
                          >
                            ≈ approximate
                          </span>
                        )}
                      </div>
                      <TrendChart points={colHistory["quality.distinct_ratio"]} color="#6366f1" />
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}
