import { type ReactNode, useState } from "react";
import { Card, Pill, Select, ThresholdBar } from "./vex";

/* The "Configure checks" workbench (Figma 51:10138): a 2×2 pillar grid (Freshness, Volume,
 * Schema, Distribution), a Data-quality custom-SQL block, and Schedule / Notification panels.
 * Each pillar's "Advanced editor" expands an in-card panel with the finer knobs. */

export type Severity = "error" | "warn" | "ignore";

export type FreshnessMode = "staleness" | "deadline";

export interface ChecksConfig {
  freshness: {
    mode: FreshnessMode;
    warnHours: number;
    errorHours: number;
    cadenceHours: number;
    source: string;
    deadlineTime: string; // "HH:MM" UTC (deadline mode)
    graceMinutes: number;
  };
  volume: { direction: "drop" | "spike" | "both"; warnPct: number; errorPct: number; baselineDays: number };
  schema: { added: Severity; removed: Severity; typeChanged: Severity; requireBaseline: boolean };
  distribution: { method: "PSI" | "KL" | "JS"; warn: number; error: number; windowDays: number };
  dq: { name: string; sql: string; warnAt: number; errorAt: number };
  schedule: string;
  scheduleTime: string; // "HH:MM" UTC when schedule === "daily"
  owner: string;
  notify: { warn: boolean; error: boolean; recovery: boolean; slack: string; email: string };
}

export interface FreshnessSuggestion {
  mode: FreshnessMode;
  source?: string;
  cadence_hours?: number;
  deadline_time?: string;
  grace_minutes?: number;
  warn_after_hours?: number;
  severe_after_hours?: number;
  typical_time?: string;
  confidence?: number;
  loads_observed?: number;
}

export const DEFAULT_CHECKS: ChecksConfig = {
  freshness: { mode: "staleness", warnHours: 24, errorHours: 72, cadenceHours: 24, source: "auto", deadlineTime: "06:00", graceMinutes: 30 },
  volume: { direction: "drop", warnPct: 20, errorPct: 50, baselineDays: 7 },
  schema: { added: "error", removed: "error", typeChanged: "error", requireBaseline: true },
  distribution: { method: "PSI", warn: 0.1, error: 0.25, windowDays: 7 },
  dq: { name: "", sql: "", warnAt: 1, errorAt: 10 },
  schedule: "daily",
  scheduleTime: "06:00",
  owner: "Data Platform",
  notify: { warn: true, error: true, recovery: false, slack: "", email: "" },
};

function PillarCard({
  icon, title, subtitle, children, advanced,
}: { icon: ReactNode; title: string; subtitle: string; children: ReactNode; advanced?: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <Card className="p-5">
      <div className="mb-3 flex items-start justify-between">
        <div className="flex items-start gap-2.5">
          <span className="mt-0.5 grid h-7 w-7 place-items-center rounded-lg bg-teal/10 text-teal-600">{icon}</span>
          <div>
            <div className="font-semibold text-navy">{title}</div>
            <div className="text-xs text-slate-400">{subtitle}</div>
          </div>
        </div>
        {advanced && (
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            className="text-xs font-medium text-teal-700 hover:underline"
          >
            {open ? "Hide advanced ▲" : "Advanced editor ↗"}
          </button>
        )}
      </div>
      {children}
      {advanced && open && (
        <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50/60 p-3">
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-slate-400">Advanced</div>
          {advanced}
        </div>
      )}
    </Card>
  );
}

function Lbl({ children }: { children: ReactNode }) {
  return <div className="mb-1 text-xs font-medium text-slate-500">{children}</div>;
}
const numCls =
  "w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal focus:ring-2 focus:ring-teal/20";

export function ConfigureChecks({
  value, onChange, context, onInferFreshness,
}: {
  value: ChecksConfig;
  onChange: (next: ChecksConfig) => void;
  context?: ReactNode;
  onInferFreshness?: () => Promise<FreshnessSuggestion | null>;
}) {
  const set = <K extends keyof ChecksConfig>(k: K, v: ChecksConfig[K]) => onChange({ ...value, [k]: v });
  const [inferring, setInferring] = useState(false);
  const [inferMsg, setInferMsg] = useState<string | null>(null);

  const infer = async () => {
    if (!onInferFreshness) return;
    setInferring(true);
    setInferMsg(null);
    try {
      const s = await onInferFreshness();
      if (!s) {
        setInferMsg("Not enough load history yet — let the monitor run a few cycles.");
        return;
      }
      onChange({
        ...value,
        freshness: {
          ...value.freshness,
          mode: s.mode,
          source: s.source ?? value.freshness.source,
          cadenceHours: s.cadence_hours ?? value.freshness.cadenceHours,
          deadlineTime: s.deadline_time ?? value.freshness.deadlineTime,
          graceMinutes: s.grace_minutes ?? value.freshness.graceMinutes,
          warnHours: s.warn_after_hours ?? value.freshness.warnHours,
          errorHours: s.severe_after_hours ?? value.freshness.errorHours,
        },
      });
      setInferMsg(
        s.mode === "deadline"
          ? `Inferred from ${s.loads_observed} loads: daily by ${s.deadline_time} UTC (±${s.grace_minutes}m grace, typical ${s.typical_time}).`
          : `Inferred from ${s.loads_observed} loads: ~${s.cadence_hours}h cadence → warn ${s.warn_after_hours}h / error ${s.severe_after_hours}h.`,
      );
    } catch (e) {
      setInferMsg((e as Error).message);
    } finally {
      setInferring(false);
    }
  };

  return (
    <div className="space-y-5">
      {context}

      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm">
          <span className="font-semibold text-teal-700">Shared defaults</span>
          <span className="text-slate-500"> · every table inherits these thresholds</span>
        </div>
        <div className="flex gap-4 text-xs font-medium text-teal-700">
          <button className="hover:underline">Simple setup</button>
          <button className="hover:underline">Search tables &amp; overrides</button>
        </div>
      </div>

      {/* 2×2 pillar grid */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* Freshness */}
        <PillarCard icon="🕐" title="Freshness" subtitle="Time since data last arrived"
          advanced={
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Lbl>Freshness source</Lbl>
                <Select value={value.freshness.source}
                  onChange={(e) => set("freshness", { ...value.freshness, source: e.target.value })}>
                  <option value="auto">Auto</option>
                  <option value="watermark">Watermark column (MAX of the load-time column)</option>
                  <option value="file_arrival">File arrival time</option>
                </Select>
              </div>
            </div>
          }
        >
          <div className="mb-3 flex flex-wrap items-center gap-3">
            <div className="inline-flex rounded-lg border border-slate-200 p-0.5 text-xs">
              {(["staleness", "deadline"] as const).map((m) => (
                <button key={m} type="button"
                  onClick={() => set("freshness", { ...value.freshness, mode: m })}
                  className={`rounded-md px-3 py-1 font-medium transition ${value.freshness.mode === m ? "bg-navy text-white" : "text-slate-500 hover:text-slate-700"}`}>
                  {m === "staleness" ? "Staleness" : "Daily deadline"}
                </button>
              ))}
            </div>
            {onInferFreshness && (
              <button type="button" onClick={infer} disabled={inferring}
                className="text-xs font-medium text-teal-700 hover:underline disabled:opacity-50">
                {inferring ? "Inferring…" : "Infer from load history ↻"}
              </button>
            )}
          </div>
          {inferMsg && <p className="mb-3 rounded-lg bg-teal-50 px-3 py-2 text-xs text-teal-800">{inferMsg}</p>}

          {value.freshness.mode === "deadline" ? (
            <>
              <p className="mb-3 text-sm text-slate-600">
                The data must arrive by a <span className="font-medium text-slate-700">wall-clock time each day</span>.
                Past the deadline + grace with no new load, it warns, then escalates.
              </p>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <Lbl>Data due by (UTC, daily)</Lbl>
                  <input type="time" className={numCls} value={value.freshness.deadlineTime}
                    onChange={(e) => set("freshness", { ...value.freshness, deadlineTime: e.target.value })} />
                </div>
                <div>
                  <Lbl>Grace</Lbl>
                  <div className="flex items-center gap-2">
                    <input type="number" className={numCls} value={value.freshness.graceMinutes}
                      onChange={(e) => set("freshness", { ...value.freshness, graceMinutes: Number(e.target.value) })} />
                    <span className="text-xs text-slate-400">min</span>
                  </div>
                </div>
              </div>
              <p className="mt-3 text-xs text-slate-400">
                “by {value.freshness.deadlineTime} UTC, {value.freshness.graceMinutes}m grace”: if today's load hasn't
                arrived by {value.freshness.deadlineTime}&nbsp;+&nbsp;{value.freshness.graceMinutes}m it warns, then escalates.
                Make sure the monitor's schedule runs after the deadline.
              </p>
            </>
          ) : (
            <>
              <p className="mb-3 text-sm text-slate-600">
                The clock starts at the <span className="font-medium text-slate-700">last load</span>. Thresholds are
                <span className="font-medium text-slate-700"> hours of staleness since the last load</span>.
              </p>
              <div className="grid grid-cols-3 gap-3">
                <div>
                  <Lbl>Loads every</Lbl>
                  <div className="flex items-center gap-2">
                    <input type="number" className={numCls} value={value.freshness.cadenceHours}
                      onChange={(e) => set("freshness", { ...value.freshness, cadenceHours: Number(e.target.value) })} />
                    <span className="text-xs text-slate-400">hrs</span>
                  </div>
                </div>
                <div>
                  <Lbl>Warn if stale for</Lbl>
                  <div className="flex items-center gap-2">
                    <input type="number" className={numCls} value={value.freshness.warnHours}
                      onChange={(e) => set("freshness", { ...value.freshness, warnHours: Number(e.target.value) })} />
                    <span className="text-xs text-slate-400">hrs</span>
                  </div>
                </div>
                <div>
                  <Lbl>Error if stale for</Lbl>
                  <div className="flex items-center gap-2">
                    <input type="number" className={numCls} value={value.freshness.errorHours}
                      onChange={(e) => set("freshness", { ...value.freshness, errorHours: Number(e.target.value) })} />
                    <span className="text-xs text-slate-400">hrs</span>
                  </div>
                </div>
              </div>
              <button type="button"
                onClick={() => {
                  const c = Math.max(1, value.freshness.cadenceHours || 24);
                  set("freshness", { ...value.freshness, warnHours: Math.ceil(c * 1.25), errorHours: Math.ceil(c * 2) });
                }}
                className="mt-2 text-xs font-medium text-teal-700 hover:underline">
                Suggest thresholds from load frequency ↻
              </button>
              <div className="mt-3">
                <ThresholdBar labels={["Last load", `${value.freshness.warnHours}h stale · Warn`, `${value.freshness.errorHours}h stale · Error`]} />
              </div>
              <p className="mt-2 text-xs text-slate-400">
                A <span className="font-medium">6-hour</span> load → loads every 6h, warn ~8h, error ~12h. A daily load → 24h / 72h.
              </p>
            </>
          )}
        </PillarCard>

        {/* Volume */}
        <PillarCard icon="📊" title="Volume" subtitle="Relative change · rows per scan"
          advanced={
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Lbl>Baseline window</Lbl>
                <div className="flex items-center gap-2">
                  <input type="number" className={numCls} value={value.volume.baselineDays}
                    onChange={(e) => set("volume", { ...value.volume, baselineDays: Number(e.target.value) })} />
                  <span className="text-xs text-slate-400">days</span>
                </div>
              </div>
            </div>
          }
        >
          <p className="mb-3 text-sm text-slate-600">
            When the row count moves from its learned baseline, flag the size of the change.
          </p>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <Lbl>Direction</Lbl>
              <Select value={value.volume.direction}
                onChange={(e) => set("volume", { ...value.volume, direction: e.target.value as ChecksConfig["volume"]["direction"] })}>
                <option value="drop">Drop</option>
                <option value="spike">Spike</option>
                <option value="both">Both</option>
              </Select>
            </div>
            <div>
              <Lbl>Warning ≥</Lbl>
              <div className="flex items-center gap-1">
                <input type="number" className={numCls} value={value.volume.warnPct}
                  onChange={(e) => set("volume", { ...value.volume, warnPct: Number(e.target.value) })} />
                <span className="text-xs text-slate-400">%</span>
              </div>
            </div>
            <div>
              <Lbl>Error ≥</Lbl>
              <div className="flex items-center gap-1">
                <input type="number" className={numCls} value={value.volume.errorPct}
                  onChange={(e) => set("volume", { ...value.volume, errorPct: Number(e.target.value) })} />
                <span className="text-xs text-slate-400">%</span>
              </div>
            </div>
          </div>
          <div className="mt-3">
            <ThresholdBar labels={["0%", `${value.volume.warnPct}%`, `≥ ${value.volume.errorPct}%`]} />
          </div>
          <p className="mt-2 text-xs text-slate-400">
            Needs a few days to learn the baseline; alerts wait for a valid reference.
          </p>
        </PillarCard>

        {/* Schema */}
        <PillarCard icon="▦" title="Schema" subtitle="Strict policy · per change type"
          advanced={
            <label className="flex items-center gap-2 text-sm text-slate-600">
              <input type="checkbox" className="h-4 w-4 rounded border-slate-300 text-teal focus:ring-teal/30"
                checked={value.schema.requireBaseline}
                onChange={(e) => set("schema", { ...value.schema, requireBaseline: e.target.checked })} />
              Require a saved schema baseline before alerting
            </label>
          }
        >
          <p className="mb-3 text-sm text-slate-600">
            When a table's columns differ from the saved schema, apply a severity to each kind of change.
          </p>
          <div className="grid grid-cols-3 gap-3">
            {(["added", "removed", "typeChanged"] as const).map((k) => (
              <div key={k}>
                <Lbl>{k === "typeChanged" ? "Type changed" : k === "added" ? "Added field" : "Removed field"}</Lbl>
                <Select value={value.schema[k]}
                  onChange={(e) => set("schema", { ...value.schema, [k]: e.target.value as Severity })}>
                  <option value="error">Error</option>
                  <option value="warn">Warn</option>
                  <option value="ignore">Ignore</option>
                </Select>
              </div>
            ))}
          </div>
          <p className="mt-3 text-xs text-slate-400">The saved schema is the baseline; a confirmed change replaces it.</p>
        </PillarCard>

        {/* Distribution */}
        <PillarCard icon="📈" title="Distribution" subtitle="Drift · configured field mappings"
          advanced={
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Lbl>Reference window</Lbl>
                <div className="flex items-center gap-2">
                  <input type="number" className={numCls} value={value.distribution.windowDays}
                    onChange={(e) => set("distribution", { ...value.distribution, windowDays: Number(e.target.value) })} />
                  <span className="text-xs text-slate-400">days</span>
                </div>
              </div>
            </div>
          }
        >
          <p className="mb-3 text-sm text-slate-600">
            When values shift from the recent valid window, compare the completed period against the approved reference.
          </p>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <Lbl>Method</Lbl>
              <Select value={value.distribution.method}
                onChange={(e) => set("distribution", { ...value.distribution, method: e.target.value as ChecksConfig["distribution"]["method"] })}>
                <option value="PSI">PSI</option>
                <option value="KL">KL</option>
                <option value="JS">JS</option>
              </Select>
            </div>
            <div>
              <Lbl>Warning ≥</Lbl>
              <input type="number" step="0.01" className={numCls} value={value.distribution.warn}
                onChange={(e) => set("distribution", { ...value.distribution, warn: Number(e.target.value) })} />
            </div>
            <div>
              <Lbl>Error ≥</Lbl>
              <input type="number" step="0.01" className={numCls} value={value.distribution.error}
                onChange={(e) => set("distribution", { ...value.distribution, error: Number(e.target.value) })} />
            </div>
          </div>
          <p className="mt-3 text-xs text-slate-400">Learning and missing input are not Healthy; alerts wait for a valid baseline.</p>
        </PillarCard>
      </div>

      {/* Data quality */}
      <Card className="p-5">
        <div className="mb-3 flex items-center justify-between">
          <div>
            <div className="font-semibold text-navy">Data quality</div>
            <div className="text-xs text-slate-400">Key-integrity + custom SQL · warn on failed key</div>
          </div>
          <button className="text-xs font-medium text-teal-700 hover:underline">Open full SQL editor ↗</button>
        </div>
        <div className="rounded-lg border border-slate-200 p-3">
          <div className="grid gap-3 md:grid-cols-[1fr_2fr]">
            <div>
              <Lbl>Rule name</Lbl>
              <input className={numCls} placeholder="Non-negative order totals" value={value.dq.name}
                onChange={(e) => set("dq", { ...value.dq, name: e.target.value })} />
              <div className="mt-2 flex gap-2">
                <div>
                  <Lbl>Warn ≥</Lbl>
                  <input type="number" className={numCls} value={value.dq.warnAt}
                    onChange={(e) => set("dq", { ...value.dq, warnAt: Number(e.target.value) })} />
                </div>
                <div>
                  <Lbl>Error ≥</Lbl>
                  <input type="number" className={numCls} value={value.dq.errorAt}
                    onChange={(e) => set("dq", { ...value.dq, errorAt: Number(e.target.value) })} />
                </div>
              </div>
            </div>
            <div>
              <Lbl>Read-only SELECT — returns <code>failed_rows</code></Lbl>
              <textarea rows={8}
                className="w-full resize-y rounded-lg border border-slate-300 px-3 py-2 font-mono text-xs outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
                style={{ minHeight: "10rem" }}
                placeholder="SELECT COUNT(*) AS failed_rows FROM orders WHERE order_total < 0;"
                value={value.dq.sql}
                onChange={(e) => set("dq", { ...value.dq, sql: e.target.value })} />
              <div className="mt-1 text-[11px] text-slate-400">Pass 0 · Warning {value.dq.warnAt}–{value.dq.errorAt - 1} · Error ≥ {value.dq.errorAt} failed_rows</div>
            </div>
          </div>
        </div>
      </Card>

      {/* Schedule + Notifications */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card className="p-5">
          <div className="mb-3 font-semibold text-navy">Schedule &amp; ownership</div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Lbl>Cadence</Lbl>
              <Select value={value.schedule} onChange={(e) => set("schedule", e.target.value)}>
                <option value="hourly">Hourly · UTC</option>
                <option value="daily">Daily · UTC</option>
                <option value="manual">Manual</option>
              </Select>
            </div>
            {value.schedule === "daily" ? (
              <div>
                <Lbl>Run at (UTC)</Lbl>
                <input type="time" className={numCls} value={value.scheduleTime}
                  onChange={(e) => set("scheduleTime", e.target.value)} />
              </div>
            ) : (
              <div>
                <Lbl>Owner</Lbl>
                <Select value={value.owner} onChange={(e) => set("owner", e.target.value)}>
                  <option>Data Platform</option>
                  <option>Finance Data</option>
                  <option>Growth Data</option>
                  <option>Integrations</option>
                </Select>
              </div>
            )}
          </div>
          {value.schedule === "daily" && value.freshness.mode === "deadline" && value.scheduleTime <= value.freshness.deadlineTime && (
            <p className="mt-2 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
              The check runs at {value.scheduleTime} but the freshness deadline is {value.freshness.deadlineTime} — run the
              check <span className="font-medium">after</span> the deadline + grace so a missed load is caught.
            </p>
          )}
          <p className="mt-3 text-xs text-slate-400">Run the daily check a bit after your load's deadline so late/missing loads are detected promptly.</p>
        </Card>

        <Card className="p-5">
          <div className="mb-3 flex items-center justify-between">
            <div className="font-semibold text-navy">Notification preferences</div>
            <button className="text-xs font-medium text-teal-700 hover:underline">Edit routing →</button>
          </div>
          <div className="flex flex-wrap gap-4 text-sm">
            <label className="flex items-center gap-2 text-slate-600">
              <input type="checkbox" className="h-4 w-4 rounded border-slate-300 text-teal focus:ring-teal/30"
                checked={value.notify.warn} onChange={(e) => set("notify", { ...value.notify, warn: e.target.checked })} />
              Warning
            </label>
            <label className="flex items-center gap-2 text-slate-600">
              <input type="checkbox" className="h-4 w-4 rounded border-slate-300 text-teal focus:ring-teal/30"
                checked={value.notify.error} onChange={(e) => set("notify", { ...value.notify, error: e.target.checked })} />
              Error
            </label>
            <label className="flex items-center gap-2 text-slate-600">
              <input type="checkbox" className="h-4 w-4 rounded border-slate-300 text-teal focus:ring-teal/30"
                checked={value.notify.recovery} onChange={(e) => set("notify", { ...value.notify, recovery: e.target.checked })} />
              Recovery
            </label>
          </div>
          <div className="mt-3 grid grid-cols-2 gap-3">
            <div><Lbl>Slack channel</Lbl><input className={numCls} placeholder="#data-alerts" value={value.notify.slack}
              onChange={(e) => set("notify", { ...value.notify, slack: e.target.value })} /></div>
            <div><Lbl>Email</Lbl><input className={numCls} placeholder="team@datavalidator.com" value={value.notify.email}
              onChange={(e) => set("notify", { ...value.notify, email: e.target.value })} /></div>
          </div>
          <p className="mt-3 text-xs text-slate-400">Error: immediate · Warning: after 1 failed run · repeated failures stay in one incident.</p>
        </Card>
      </div>
    </div>
  );
}

// Translate the UI config into the backend AlertPolicy shape (schema/distribution included).
export function checksToAlertPolicy(c: ChecksConfig) {
  return {
    freshness: {
      source: c.freshness.source,
      mode: c.freshness.mode,
      cadence_hours: c.freshness.cadenceHours,
      warn_after_hours: c.freshness.warnHours,
      severe_after_hours: c.freshness.errorHours,
      deadline_time: c.freshness.mode === "deadline" ? c.freshness.deadlineTime : null,
      grace_minutes: c.freshness.graceMinutes,
    },
    quality: { warn_below: 80, severe_below: 60 },
    volume: { warn_pct: c.volume.warnPct, severe_pct: c.volume.errorPct },
    schema: { added: c.schema.added, removed: c.schema.removed, type_changed: c.schema.typeChanged },
    distribution: { method: c.distribution.method, warn: c.distribution.warn, error: c.distribution.error },
  };
}

// ─── Per-table threshold overrides ───────────────────────────────────────────
// A partial alert policy stored on ONE table (TableMetadata.config.alert_policy). It overlays
// the monitor default field-by-field on the backend, so a table tweaks only what it sets.
export type PolicyOverride = {
  freshness?: { warn_after_hours?: number; severe_after_hours?: number };
  volume?: { warn_pct?: number; severe_pct?: number };
  quality?: { warn_below?: number };
};

const OV_COLS = [
  { pillar: "freshness", field: "warn_after_hours", label: "Fresh warn (h)" },
  { pillar: "freshness", field: "severe_after_hours", label: "Fresh error (h)" },
  { pillar: "volume", field: "warn_pct", label: "Vol warn (%)" },
  { pillar: "volume", field: "severe_pct", label: "Vol error (%)" },
  { pillar: "quality", field: "warn_below", label: "Quality min" },
] as const;

/** Grid of per-table threshold overrides. The monitor defaults apply to every table; a value
 *  typed here overrides that one threshold for that one table (blank = inherit). Built for
 *  big scopes: search + scroll, and only the exceptions are stored. */
export function PerTableOverrides({ tables, value, onChange, defaults }: {
  tables: string[];
  value: Record<string, PolicyOverride>;
  onChange: (next: Record<string, PolicyOverride>) => void;
  defaults: ChecksConfig;
}) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const placeholders: Record<string, number> = {
    "freshness.warn_after_hours": defaults.freshness.warnHours,
    "freshness.severe_after_hours": defaults.freshness.errorHours,
    "volume.warn_pct": defaults.volume.warnPct,
    "volume.severe_pct": defaults.volume.errorPct,
    "quality.warn_below": 80,
  };
  const overriddenCount = Object.keys(value).length;
  const shown = tables.filter((t) => t.toLowerCase().includes(q.trim().toLowerCase()));

  const getCell = (t: string, pillar: string, field: string): number | undefined =>
    (value[t]?.[pillar as keyof PolicyOverride] as Record<string, number> | undefined)?.[field];

  const setCell = (t: string, pillar: string, field: string, raw: string) => {
    const cur: PolicyOverride = { ...(value[t] ?? {}) };
    const pil = { ...((cur[pillar as keyof PolicyOverride] as Record<string, number>) ?? {}) };
    if (raw === "" || isNaN(Number(raw))) delete pil[field]; else pil[field] = Number(raw);
    if (Object.keys(pil).length) (cur as Record<string, unknown>)[pillar] = pil;
    else delete (cur as Record<string, unknown>)[pillar];
    const out = { ...value };
    if (Object.keys(cur).length) out[t] = cur; else delete out[t];
    onChange(out);
  };
  const clearRow = (t: string) => { const out = { ...value }; delete out[t]; onChange(out); };

  return (
    <Card className="p-5">
      <button type="button" onClick={() => setOpen((o) => !o)} className="flex w-full items-center justify-between">
        <div className="text-left">
          <div className="text-sm font-bold uppercase tracking-wide text-navy">Per-table overrides <span className="font-normal normal-case text-slate-400">— optional</span></div>
          <p className="mt-1 text-xs text-slate-400">
            The checks above apply to all {tables.length} tables. Override a threshold for specific tables here — blank inherits the default.
            {overriddenCount > 0 && <span className="ml-1 font-medium text-teal-700">{overriddenCount} overridden</span>}
          </p>
        </div>
        <span className="text-slate-400">{open ? "▲" : "▼"}</span>
      </button>

      {open && (
        <div className="mt-4 space-y-2">
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="filter tables…"
            className="w-64 rounded-lg border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-teal" />
          <div className="max-h-80 overflow-auto rounded-lg border border-slate-200">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-400">
                <tr>
                  <th className="px-3 py-2 font-semibold">Table</th>
                  {OV_COLS.map((c) => <th key={c.label} className="px-2 py-2 font-semibold">{c.label}</th>)}
                  <th className="px-2 py-2" />
                </tr>
              </thead>
              <tbody>
                {shown.map((t) => {
                  const dirty = !!value[t];
                  return (
                    <tr key={t} className={`border-t border-slate-100 ${dirty ? "bg-teal/5" : ""}`}>
                      <td className="px-3 py-1.5 font-mono text-xs text-navy">{t.split(".").pop()}</td>
                      {OV_COLS.map((c) => (
                        <td key={c.label} className="px-2 py-1.5">
                          <input type="number" value={getCell(t, c.pillar, c.field) ?? ""}
                            onChange={(e) => setCell(t, c.pillar, c.field, e.target.value)}
                            placeholder={String(placeholders[`${c.pillar}.${c.field}`] ?? "")}
                            className="w-20 rounded border border-slate-300 px-1.5 py-1 text-xs outline-none focus:border-teal" />
                        </td>
                      ))}
                      <td className="px-2 py-1.5 text-right">
                        {dirty && <button onClick={() => clearRow(t)} className="text-[11px] text-slate-400 hover:text-rose-500">reset</button>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="text-[11px] text-slate-400">Placeholders show the monitor default. Only tables you change are stored as overrides.</p>
        </div>
      )}
    </Card>
  );
}

// Reverse of checksToAlertPolicy — seed the wizard from an existing monitor's stored policy.
export function alertPolicyToChecks(p: Record<string, unknown> | null | undefined): ChecksConfig {
  const g = (k: string) => (p?.[k] ?? {}) as Record<string, unknown>;
  const f = g("freshness"), v = g("volume"), s = g("schema"), d = g("distribution");
  const num = (x: unknown, dflt: number) => (typeof x === "number" ? x : dflt);
  return {
    ...DEFAULT_CHECKS,
    freshness: {
      mode: (f.mode as FreshnessMode) === "deadline" ? "deadline" : "staleness",
      warnHours: num(f.warn_after_hours, 24),
      errorHours: num(f.severe_after_hours, 72),
      cadenceHours: num(f.cadence_hours, 24),
      source: (f.source as string) ?? "auto",
      deadlineTime: (f.deadline_time as string) || "06:00",
      graceMinutes: num(f.grace_minutes, 30),
    },
    volume: {
      direction: DEFAULT_CHECKS.volume.direction,
      warnPct: num(v.warn_pct, 20),
      errorPct: num(v.severe_pct, 50),
      baselineDays: num(v.baseline_days, 7),
    },
    schema: {
      added: (s.added as Severity) ?? "error",
      removed: (s.removed as Severity) ?? "error",
      typeChanged: (s.type_changed as Severity) ?? "error",
      requireBaseline: s.require_baseline !== false,
    },
    distribution: {
      method: (d.method as ChecksConfig["distribution"]["method"]) ?? "PSI",
      warn: num(d.warn, 0.1),
      error: num(d.error, 0.25),
      windowDays: num(d.window_days, 7),
    },
  };
}

// Pillar-count badge for the header ("16 checks · 5 pillars" in the Figma).
export function CheckCount({ checks, pillars }: { checks: number; pillars: number }) {
  return <Pill tone="neutral" dot={false}>{checks} checks · {pillars} pillars</Pill>;
}
