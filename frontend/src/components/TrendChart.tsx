import {
  Area,
  CartesianGrid,
  ComposedChart,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { HistoryPoint } from "../types";

function fmtTs(ts: string) {
  return new Date(ts).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function TrendChart({
  points,
  color = "#17a2b8",
  height = 150,
  band = false,
}: {
  points: HistoryPoint[];
  color?: string;
  height?: number;
  band?: boolean; // overlay the expected baseline band + mark anomalies
}) {
  if (points.length === 0)
    return (
      <div className="grid place-items-center text-xs text-slate-400" style={{ height }}>
        No history yet
      </div>
    );
  const hasBand = band && points.some((p) => p.lo != null && p.hi != null);
  const data = points.map((p) => ({
    ts: fmtTs(p.ts),
    value: p.value,
    // band drawn as [lo, hi-lo] stacked areas; anomaly points surfaced as a scatter series
    lo: hasBand ? (p.lo ?? null) : null,
    span: hasBand && p.lo != null && p.hi != null ? p.hi - p.lo : null,
    anomaly: p.anomaly ? p.value : null,
  }));
  const single = data.length === 1;
  const gid = `g-${color.replace("#", "")}`;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity={0.25} />
            <stop offset="100%" stopColor={color} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
        <XAxis dataKey="ts" tick={{ fontSize: 11 }} stroke="#94a3b8" />
        <YAxis tick={{ fontSize: 11 }} stroke="#94a3b8" width={52} />
        <Tooltip />
        {hasBand && (
          <>
            {/* expected band: transparent base up to `lo`, then a shaded `span` to `hi` */}
            <Area dataKey="lo" stackId="band" stroke="none" fill="none" isAnimationActive={false} />
            <Area
              dataKey="span"
              stackId="band"
              stroke="none"
              fill="#94a3b8"
              fillOpacity={0.14}
              isAnimationActive={false}
            />
          </>
        )}
        <Area
          type="monotone"
          dataKey="value"
          stroke={color}
          strokeWidth={2}
          fill={`url(#${gid})`}
          dot={single ? { r: 4, fill: color } : { r: 2 }}
          isAnimationActive={false}
        />
        {hasBand && (
          <Scatter dataKey="anomaly" fill="#ef4444" shape="circle" isAnimationActive={false} />
        )}
      </ComposedChart>
    </ResponsiveContainer>
  );
}
