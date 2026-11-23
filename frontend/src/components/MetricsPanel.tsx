import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { HistoryPoint, Metric, MetricObservation } from "../types";
import { Button, Card, HealthBadge, Spinner } from "./ui";
import { TrendChart } from "./TrendChart";

const statusTone = (s: string): "healthy" | "warning" | "critical" | "unknown" =>
  s === "pass" ? "healthy" : s === "warn" ? "warning" : s === "fail" ? "critical" : "unknown";

// Build a trend series from observations: value line + baseline band (lo/hi from the rule's
// limit around its baseline) + mark failed points so the chart highlights them (FR-U2).
function toPoints(obs: MetricObservation[]): HistoryPoint[] {
  return [...obs]
    .reverse() // API returns newest-first; chart wants oldest→newest
    .filter((o) => !o.excluded && o.value != null)
    .map((o) => {
      const rule = (o.detail?.rules ?? []).find((r) => r.baseline_value != null);
      const base = rule?.baseline_value ?? null;
      let lo: number | null = null;
      let hi: number | null = null;
      if (base != null && rule?.limit && rule.limit.endsWith("%")) {
        const pct = parseFloat(rule.limit) / 100;
        lo = base * (1 - pct);
        hi = base * (1 + pct);
      }
      return {
        ts: o.captured_at ?? "",
        value: o.value as number,
        baseline: base,
        lo,
        hi,
        anomaly: o.status === "fail" || o.status === "warn",
      };
    });
}

function MetricCard({ metric, onChange }: { metric: Metric; onChange: () => void }) {
  const [obs, setObs] = useState<MetricObservation[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(() => {
    api.metricObservations(metric.id).then((r) => setObs(r.data)).catch((e) => setErr((e as Error).message));
  }, [metric.id]);
  useEffect(load, [load]);

  const latest = obs?.[0];
  const run = async () => {
    setBusy(true);
    setErr(null);
    try {
      await api.runMetric(metric.id, { run_id: `manual-${Date.now()}` });
      load();
      onChange();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const exclude = async (o: MetricObservation) => {
    const reason = window.prompt("Exclude this point from the baseline — reason?", "known outage");
    if (reason == null) return;
    await api.excludeObservation(o.id, reason).catch((e) => setErr((e as Error).message));
    load();
  };
  const accept = async (o: MetricObservation) => {
    await api.acceptObservation(o.id).catch((e) => setErr((e as Error).message));
    load();
  };

  return (
    <Card className="space-y-3 p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-medium text-navy">{metric.name}</span>
            {latest && <HealthBadge status={statusTone(latest.status)} label={latest.status} />}
          </div>
          <div className="mt-0.5 text-xs text-slate-400">
            {metric.source_type.toUpperCase()}
            {latest?.value != null ? ` · last ${latest.value.toLocaleString()}` : " · no runs yet"}
            {(metric.rules?.length ?? 0) > 0 ? ` · ${metric.rules!.length} rule${metric.rules!.length === 1 ? "" : "s"}` : ""}
          </div>
        </div>
        <Button variant="ghost" onClick={run} disabled={busy}>
          {busy ? "Running…" : "Run now"}
        </Button>
      </div>

      {obs === null ? (
        <Spinner />
      ) : (
        <>
          <TrendChart points={toPoints(obs)} band height={140} />
          {latest && latest.detail?.rules?.some((r) => r.message) && (
            <ul className="space-y-0.5 text-xs">
              {latest.detail.rules
                .filter((r) => r.message)
                .map((r, i) => (
                  <li key={i} className={r.status === "pass" ? "text-slate-500" : r.status === "fail" ? "text-rose-600" : "text-amber-600"}>
                    {r.status === "pass" ? "✓" : "✗"} {r.message}
                  </li>
                ))}
            </ul>
          )}
          {/* Recent points with exclude/accept (FR-U3) */}
          {obs.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {obs.slice(0, 8).map((o) => (
                <span
                  key={o.id}
                  title={o.captured_at ?? ""}
                  className={`group inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] ${
                    o.excluded ? "bg-slate-100 text-slate-400 line-through" : "bg-slate-50 text-slate-600"
                  }`}
                >
                  {o.value?.toLocaleString() ?? "—"}
                  {!o.excluded && (
                    <button onClick={() => exclude(o)} title="exclude from baseline" className="text-slate-300 hover:text-rose-500">
                      ×
                    </button>
                  )}
                  {o.status === "fail" && !o.accepted && (
                    <button onClick={() => accept(o)} title="accept as new normal" className="text-teal-500 hover:text-teal-700">
                      ✓
                    </button>
                  )}
                </span>
              ))}
            </div>
          )}
          {err && <p className="text-xs text-rose-600">{err}</p>}
        </>
      )}
    </Card>
  );
}

export function MetricsPanel({ monitorId, refreshSignal = 0 }: { monitorId: string; refreshSignal?: number }) {
  const [metrics, setMetrics] = useState<Metric[] | null>(null);

  const load = useCallback(() => {
    api.listMetrics({ monitorId }).then((r) => setMetrics(r.data)).catch(() => setMetrics([]));
  }, [monitorId]);
  useEffect(load, [load, refreshSignal]);

  if (metrics === null) return <Spinner />;
  if (metrics.length === 0) return null; // nothing to show until rules are added

  return (
    <div className="space-y-3">
      <div>
        <h2 className="text-base font-semibold text-navy">Baseline metrics</h2>
        <p className="mt-0.5 text-xs text-slate-400">
          Numeric values tracked over time and checked against their own history.
        </p>
      </div>
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        {metrics.map((m) => (
          <MetricCard key={m.id} metric={m} onChange={load} />
        ))}
      </div>
    </div>
  );
}
