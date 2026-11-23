/** Small SVG dataviz for the data-validation health dashboard. No deps — hand-rolled SVG. */

const OK = "#10b981";   // emerald
const WARN = "#f59e0b"; // amber
const BAD = "#f43f5e";  // rose
const IDLE = "#cbd5e1"; // slate-300

function rateColor(pct: number): string {
  return pct >= 90 ? OK : pct >= 60 ? WARN : BAD;
}

/** Donut gauge for a 0–100 pass rate. Green ≥90, amber ≥60, else rose. */
export function StatRing({ pct, size = 76, stroke = 9 }: { pct: number; size?: number; stroke?: number }) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const clamped = Math.max(0, Math.min(100, pct));
  const off = c * (1 - clamped / 100);
  return (
    <svg width={size} height={size} className="shrink-0" role="img" aria-label={`${Math.round(clamped)}% pass rate`}>
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#e2e8f0" strokeWidth={stroke} />
      <circle
        cx={size / 2} cy={size / 2} r={r} fill="none" stroke={rateColor(clamped)} strokeWidth={stroke}
        strokeDasharray={c} strokeDashoffset={off} strokeLinecap="round"
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
        style={{ transition: "stroke-dashoffset .6s ease" }}
      />
      <text x="50%" y="50%" dominantBaseline="central" textAnchor="middle"
        className="fill-navy font-bold" style={{ fontSize: size * 0.26 }}>
        {Math.round(clamped)}%
      </text>
    </svg>
  );
}

/** Daily pass-rate bars. values in 0–1; null = no runs that day (grey). */
export function Sparkline({ values, width = 150, height = 34, labels }: {
  values: (number | null)[]; width?: number; height?: number; labels?: string[];
}) {
  if (!values.length) return null;
  const n = values.length;
  const bw = width / n;
  return (
    <svg width={width} height={height} className="shrink-0" role="img" aria-label="pass rate trend">
      {values.map((v, i) => {
        const has = v !== null && v !== undefined;
        const h = has ? Math.max(2, (v as number) * (height - 2)) : 3;
        const color = !has ? IDLE : rateColor((v as number) * 100);
        return (
          <rect key={i} x={i * bw + 1} y={height - h} width={Math.max(1, bw - 2)} height={h} rx={1.5} fill={color}>
            {labels?.[i] && <title>{labels[i]}</title>}
          </rect>
        );
      })}
    </svg>
  );
}

export type RunOutcome = "pass" | "diff" | "fail";

/** Last-N run outcomes as tiny colored bars (oldest → newest). */
export function TrendDots({ results }: { results: RunOutcome[] }) {
  if (!results.length) return <span className="text-[10px] text-slate-300">no runs</span>;
  const fill: Record<RunOutcome, string> = { pass: OK, diff: BAD, fail: WARN };
  return (
    <span className="inline-flex items-end gap-[2px]" title={`last ${results.length} runs`}>
      {results.map((r, i) => (
        <span key={i} className="inline-block h-3.5 w-[3px] rounded-sm" style={{ backgroundColor: fill[r] }} />
      ))}
    </span>
  );
}

/** A compact KPI chip with a colored dot. */
export function Kpi({ label, value, tone }: { label: string; value: number; tone: "ok" | "warn" | "bad" | "idle" | "info" }) {
  const dot = { ok: "bg-emerald-500", warn: "bg-rose-500", bad: "bg-amber-500", idle: "bg-slate-300", info: "bg-sky-500" }[tone];
  return (
    <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-1.5">
      <span className={`h-2 w-2 rounded-full ${dot}`} />
      <span className="text-sm font-bold text-navy">{value}</span>
      <span className="text-xs text-slate-500">{label}</span>
    </div>
  );
}
