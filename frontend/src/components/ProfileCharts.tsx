import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { SortTh, useSort } from "./sortable";

export interface ProfileColumn {
  name: string;
  data_type?: string;
  non_null?: number;
  null_count: number;
  null_pct?: number;
  empty_count: number;
  empty_pct?: number;
  distinct: number;
  distinct_pct?: number;
  distinct_approx?: boolean;
  min?: unknown;
  max?: unknown;
}

export interface ProfileTable {
  table: string;
  row_count: number;
  columns: ProfileColumn[];
}

function ChartCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">{title}</div>
      {children}
    </div>
  );
}

// Numeric coercion for min/max which may be strings or numbers in a profile.
const asNum = (v: unknown): number | string | null =>
  v == null ? null : typeof v === "number" ? v : !isNaN(Number(v)) ? Number(v) : String(v);

export function ProfileCharts({ table }: { table: ProfileTable }) {
  const cols = table.columns;
  const { sorted: sortedCols, sort, requestSort } = useSort(cols, {
    column: (c) => c.name,
    type: (c) => c.data_type,
    nulls: (c) => c.null_count,
    empty: (c) => c.empty_count,
    unique: (c) => c.distinct,
    unique_pct: (c) => c.distinct_pct ?? null,
    min: (c) => asNum(c.min),
    max: (c) => asNum(c.max),
  });
  const n = table.row_count || 1;

  // populated vs null vs empty (stacked, as a share of rows)
  const fill = cols.map((c) => ({
    name: c.name,
    populated: Math.max(0, (c.non_null ?? n - c.null_count) - c.empty_count),
    empty: c.empty_count,
    null: c.null_count,
  }));

  // uniqueness (% distinct of rows) — a quick read on keys vs categoricals
  const uniq = cols.map((c) => ({
    name: c.name,
    unique_pct: c.distinct_pct ?? Math.round((c.distinct / n) * 1000) / 10,
    approx: !!c.distinct_approx,
  }));

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <ChartCard title="Completeness — populated · empty · null">
          <ResponsiveContainer width="100%" height={Math.max(160, cols.length * 34)}>
            <BarChart data={fill} layout="vertical" margin={{ left: 8, right: 16 }} stackOffset="none">
              <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
              <XAxis type="number" tick={{ fontSize: 11 }} stroke="#94a3b8" />
              <YAxis type="category" dataKey="name" width={90} tick={{ fontSize: 11 }} stroke="#94a3b8" />
              <Tooltip />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Bar dataKey="populated" stackId="a" fill="#14b8a6" />
              <Bar dataKey="empty" stackId="a" fill="#f59e0b" />
              <Bar dataKey="null" stackId="a" fill="#ef4444" />
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard title="Uniqueness (% distinct of rows)">
          <ResponsiveContainer width="100%" height={Math.max(160, cols.length * 34)}>
            <BarChart data={uniq} layout="vertical" margin={{ left: 8, right: 28 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
              <XAxis type="number" domain={[0, 100]} tick={{ fontSize: 11 }} stroke="#94a3b8" />
              <YAxis type="category" dataKey="name" width={90} tick={{ fontSize: 11 }} stroke="#94a3b8" />
              <Tooltip formatter={(v: number, _n, p) => [`${v}%${p?.payload?.approx ? " (approx)" : ""}`, "unique"]} />
              <Bar dataKey="unique_pct" radius={[0, 3, 3, 0]}>
                <LabelList dataKey="unique_pct" position="right" style={{ fontSize: 10, fill: "#64748b" }} />
                {uniq.map((u, i) => (
                  <Cell key={i} fill={u.unique_pct >= 95 ? "#0ea5e9" : u.unique_pct >= 20 ? "#6366f1" : "#94a3b8"} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
      </div>

      <div className="overflow-x-auto rounded-lg border border-slate-200">
        <table className="w-full text-xs">
          <thead className="border-b border-slate-200 bg-slate-50 text-left text-slate-400">
            <tr>
              {(
                [
                  ["Column", "column"],
                  ["Type", "type"],
                  ["Nulls", "nulls"],
                  ["Empty", "empty"],
                  ["Unique", "unique"],
                  ["Unique %", "unique_pct"],
                  ["Min", "min"],
                  ["Max", "max"],
                ] as const
              ).map(([label, key]) => (
                <SortTh key={key} label={label} columnKey={key} sort={sort} onSort={requestSort} className="px-3 py-2 font-medium" />
              ))}
            </tr>
          </thead>
          <tbody>
            {sortedCols.map((c) => (
              <tr key={c.name} className="border-b border-slate-100 last:border-0">
                <td className="px-3 py-1.5 font-mono text-navy">{c.name}</td>
                <td className="px-3 py-1.5 text-slate-500">{c.data_type}</td>
                <td className="px-3 py-1.5">{c.null_count}</td>
                <td className="px-3 py-1.5">{c.empty_count}</td>
                <td className="px-3 py-1.5">
                  {c.distinct_approx ? "~" : ""}
                  {c.distinct.toLocaleString()}
                </td>
                <td className="px-3 py-1.5">{c.distinct_pct ?? "—"}%</td>
                <td className="px-3 py-1.5 font-mono text-slate-500">{String(c.min ?? "")}</td>
                <td className="px-3 py-1.5 font-mono text-slate-500">{String(c.max ?? "")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {cols.some((c) => c.distinct_approx) && (
        <p className="text-[11px] leading-snug text-slate-400">
          <span className="font-medium text-slate-500">~</span> Unique counts marked{" "}
          <span className="font-mono">~</span> are <span className="font-medium">approximate</span>{" "}
          — computed with the warehouse's HyperLogLog (APPROX_COUNT_DISTINCT) to save cost on large
          columns. Small columns are counted exactly.
        </p>
      )}
    </div>
  );
}
