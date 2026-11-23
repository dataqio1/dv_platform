import { useMemo, useState, type ReactNode } from "react";

/**
 * Reusable sortable-table primitive — the house standard for clickable column headers.
 *
 * - `useSort(rows, accessors, initial)` returns the sorted rows + current sort + a toggler.
 * - `<SortTh>` renders an accessible header cell (button, arrow indicator, aria-sort). Columns
 *   with no accessor stay plain (e.g. an actions column), so headers never promise sorting they
 *   can't deliver.
 *
 * Comparison is type-aware and stable: numbers numerically, strings via locale-aware natural
 * compare ("item2" < "item10"), and empty/null values always sort last regardless of direction.
 * Status/health columns sort by passing a rank accessor (e.g. critical→0, healthy→2).
 */

export type SortValue = string | number | null | undefined;
export type SortDir = "asc" | "desc";
export interface SortState {
  key: string;
  dir: SortDir;
}

function compare(a: SortValue, b: SortValue, dirMul: number): number {
  const an = a == null || a === "";
  const bn = b == null || b === "";
  if (an && bn) return 0;
  if (an) return 1; // nulls/blanks last, both directions
  if (bn) return -1;
  const base =
    typeof a === "number" && typeof b === "number"
      ? a - b
      : String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
  return dirMul * base;
}

export function useSort<T>(
  rows: T[],
  accessors: Record<string, (row: T) => SortValue>,
  initial?: SortState,
) {
  const [sort, setSort] = useState<SortState | null>(initial ?? null);

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const acc = accessors[sort.key];
    if (!acc) return rows;
    const dirMul = sort.dir === "asc" ? 1 : -1;
    // Array.prototype.sort is stable in modern engines → equal keys keep their prior order.
    return [...rows].sort((a, b) => compare(acc(a), acc(b), dirMul));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, sort]);

  // Click a header: sort asc; click the active header again: flip to desc, then back to asc.
  const requestSort = (key: string) =>
    setSort((s) => (s && s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));

  return { sorted, sort, requestSort, setSort };
}

function Arrow({ dir }: { dir: SortDir | undefined }) {
  // Active: solid up/down. Inactive: faint double-chevron that brightens on hover (affordance).
  if (!dir) {
    return (
      <svg viewBox="0 0 16 16" className="h-3 w-3 text-slate-300 group-hover:text-slate-400" aria-hidden>
        <path d="M8 3l3 3H5zM8 13l-3-3h6z" fill="currentColor" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 16 16" className="h-3 w-3 text-teal-600" aria-hidden>
      {dir === "asc" ? <path d="M8 4l4 5H4z" fill="currentColor" /> : <path d="M8 12l-4-5h8z" fill="currentColor" />}
    </svg>
  );
}

export function SortTh({
  label,
  columnKey,
  sort,
  onSort,
  sortable = true,
  align = "left",
  className = "",
}: {
  label: ReactNode;
  columnKey?: string;
  sort: SortState | null;
  onSort: (key: string) => void;
  sortable?: boolean;
  align?: "left" | "right" | "center";
  className?: string;
}) {
  const canSort = sortable && !!columnKey;
  const active = canSort && sort?.key === columnKey;
  const dir = active ? sort!.dir : undefined;
  const justify = align === "right" ? "justify-end" : align === "center" ? "justify-center" : "justify-start";
  const textAlign = align === "right" ? "text-right" : align === "center" ? "text-center" : "text-left";

  if (!canSort) {
    return <th className={`${textAlign} ${className}`}>{label}</th>;
  }
  return (
    <th
      aria-sort={active ? (dir === "asc" ? "ascending" : "descending") : "none"}
      className={`${textAlign} ${className}`}
    >
      <button
        type="button"
        onClick={() => onSort(columnKey!)}
        className={`group inline-flex items-center gap-1 ${justify} w-full select-none hover:text-navy ${
          active ? "text-navy" : ""
        }`}
        title={`Sort by ${typeof label === "string" ? label : columnKey}`}
      >
        <span>{label}</span>
        <Arrow dir={dir} />
      </button>
    </th>
  );
}

/**
 * SortControl — a compact "Sort by ▾" dropdown + direction toggle for card grids and lists that
 * have no column headers to click. Same comparator/stability as the table headers, so sorting is
 * consistent everywhere. Pass the same `sort`/`onSort` from `useSort`.
 */
export function SortControl({
  options,
  sort,
  onSort,
  defaultLabel = "default",
}: {
  options: { key: string; label: string }[];
  sort: SortState | null;
  onSort: (key: string) => void;
  defaultLabel?: string;
}) {
  const active = options.find((o) => o.key === sort?.key);
  return (
    <div className="inline-flex items-center gap-1 text-sm">
      <span className="text-xs text-slate-400">Sort</span>
      <select
        value={sort?.key ?? ""}
        onChange={(e) => e.target.value && onSort(e.target.value)}
        className="rounded-lg border border-slate-300 px-2 py-1 text-sm outline-none focus:border-teal"
      >
        <option value="">{defaultLabel}</option>
        {options.map((o) => (
          <option key={o.key} value={o.key}>
            {o.label}
          </option>
        ))}
      </select>
      {active && (
        <button
          type="button"
          onClick={() => onSort(active.key)}
          title={sort?.dir === "asc" ? "Ascending — click for descending" : "Descending — click for ascending"}
          className="rounded-md border border-slate-200 px-1.5 py-1 text-slate-500 hover:text-navy"
          aria-label={`Toggle sort direction (currently ${sort?.dir === "asc" ? "ascending" : "descending"})`}
        >
          <Arrow dir={sort?.dir} />
        </button>
      )}
    </div>
  );
}
