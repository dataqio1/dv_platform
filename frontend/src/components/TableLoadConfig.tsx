import type { PatternFieldSpec, PatternSpec, TableColumn, TableLoadConfig as Cfg } from "../types";
import { Button, Field, TextInput } from "./ui";
import { Combobox } from "./Combobox";

/**
 * Per-table load configuration captured at test-case creation: how is THIS table loaded
 * (truncate & load, incremental append/CDC, SCD Type 2, partitioned, reference…), plus the
 * pattern-specific columns it needs. Fully data-driven from the backend pattern-spec catalog,
 * so adding a new load pattern later needs no change here.
 */

export interface PatternSpecs {
  patterns: PatternSpec[];
  fields: Record<string, PatternFieldSpec>;
}

// A load pattern → the monitor-level load_type the runner understands.
const PATTERN_TO_LOAD_TYPE: Record<string, string> = {
  full_snapshot: "full_reload",
  reference: "full_reload",
  append: "append",
  cdc: "incremental",
  scd2: "incremental",
  partitioned: "incremental",
  unknown: "incremental",
};

export const patternToLoadType = (pattern: string): string =>
  PATTERN_TO_LOAD_TYPE[pattern] ?? "incremental";

export function TableLoadConfigRow({
  table,
  columns,
  loadingColumns,
  specs,
  value,
  onChange,
  onAutoDetect,
  detecting,
  selected,
  onToggleSelect,
}: {
  table: string;
  columns: TableColumn[];
  loadingColumns: boolean;
  specs: PatternSpecs;
  value: Cfg;
  onChange: (next: Cfg) => void;
  onAutoDetect?: () => void;
  detecting?: boolean;
  selected?: boolean;
  onToggleSelect?: (on: boolean) => void;
}) {
  const spec = specs.patterns.find((p) => p.pattern === value.load_pattern);
  const colOptions = columns.map((c) => ({ value: c.name, label: `${c.name}  ·  ${c.data_type}` }));
  const set = (patch: Partial<Cfg>) => onChange({ ...value, ...patch });

  const renderField = (fieldKey: string) => {
    const fs = specs.fields[fieldKey];
    if (!fs) return null;
    const current = (value as unknown as Record<string, unknown>)[fieldKey];
    if (fs.input === "column") {
      return (
        <Field key={fieldKey} label={fs.label} hint={fs.help}>
          <Combobox
            value={(current as string) ?? ""}
            onChange={(v) => set({ [fieldKey]: v } as Partial<Cfg>)}
            options={colOptions}
            loading={loadingColumns}
            placeholder={loadingColumns ? "loading columns…" : "select or type a column"}
          />
        </Field>
      );
    }
    if (fs.input === "enum") {
      return (
        <Field key={fieldKey} label={fs.label} hint={fs.help}>
          <Combobox
            value={(current as string) ?? fs.options?.[0] ?? ""}
            onChange={(v) => set({ [fieldKey]: v } as Partial<Cfg>)}
            options={fs.options ?? []}
            allowCustom={false}
          />
        </Field>
      );
    }
    if (fs.input === "columns") {
      // Multi-column role (e.g. a composite business key): comma-separated picker of column names.
      const vals = Array.isArray(current) ? (current as string[]) : [];
      return (
        <Field key={fieldKey} label={fs.label} hint={fs.help ?? "one or more columns, comma-separated"}>
          <TextInput
            value={vals.join(", ")}
            onChange={(e) =>
              set({ [fieldKey]: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) } as Partial<Cfg>)
            }
            placeholder="e.g. CUSTOMER_ID"
          />
        </Field>
      );
    }
    if (fs.input === "bool") {
      return (
        <Field key={fieldKey} label={fs.label} hint={fs.help}>
          <label className="inline-flex items-center gap-2 text-[13px] text-slate-600">
            <input
              type="checkbox"
              checked={current === true}
              onChange={(e) => set({ [fieldKey]: e.target.checked } as Partial<Cfg>)}
            />
            <span>enabled</span>
          </label>
        </Field>
      );
    }
    if (fs.input === "text") {
      return (
        <Field key={fieldKey} label={fs.label} hint={fs.help}>
          <TextInput
            value={(current as string) ?? ""}
            onChange={(e) => set({ [fieldKey]: e.target.value } as Partial<Cfg>)}
            placeholder="e.g. 9999-12-31"
          />
        </Field>
      );
    }
    // number
    return (
      <Field key={fieldKey} label={fs.label} hint={fs.help}>
        <TextInput
          type="number"
          value={current == null ? "" : String(current)}
          onChange={(e) =>
            set({ [fieldKey]: e.target.value === "" ? null : Number(e.target.value) } as Partial<Cfg>)
          }
          placeholder="e.g. 86400"
        />
      </Field>
    );
  };

  return (
    <div className={`rounded-lg border p-3 ${selected ? "border-teal/50 bg-teal/5" : "border-slate-200"}`}>
      <div className="mb-2 flex items-center justify-between gap-2">
        <label className="flex items-center gap-2">
          {onToggleSelect && (
            <input type="checkbox" checked={!!selected} onChange={(e) => onToggleSelect(e.target.checked)} title="select for bulk apply" />
          )}
          <span className="font-mono text-sm font-medium text-navy">{table}</span>
        </label>
        {onAutoDetect && (
          <Button variant="ghost" onClick={onAutoDetect} disabled={detecting || loadingColumns}>
            {detecting ? "Detecting…" : "Auto-detect"}
          </Button>
        )}
      </div>

      <Field label="Load pattern">
        <Combobox
          value={value.load_pattern}
          onChange={(v) => set({ load_pattern: v })}
          allowCustom={false}
          options={specs.patterns.map((p) => ({ value: p.pattern, label: p.label }))}
        />
      </Field>
      {spec?.description && <p className="mt-1 text-xs text-slate-400">{spec.description}</p>}

      {spec && spec.fields.length > 0 && (
        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
          {spec.fields.map(renderField)}
        </div>
      )}
    </div>
  );
}

/** Set one load pattern (+ its fields) and apply it to many selected tables at once — so a
 *  120-table source doesn't have to be configured one row at a time. Column fields are typed
 *  (a name shared across the selected tables, e.g. BATCH_ID) rather than picked per catalog. */
export function BulkApplyPanel({
  specs,
  value,
  onChange,
  onApply,
  count,
  total,
  onSelectAll,
  onSelectNone,
}: {
  specs: PatternSpecs;
  value: Cfg;
  onChange: (next: Cfg) => void;
  onApply: () => void;
  count: number;
  total: number;
  onSelectAll: () => void;
  onSelectNone: () => void;
}) {
  const spec = specs.patterns.find((p) => p.pattern === value.load_pattern);
  const set = (patch: Partial<Cfg>) => onChange({ ...value, ...patch });

  const renderField = (fieldKey: string) => {
    const fs = specs.fields[fieldKey];
    if (!fs) return null;
    const current = (value as unknown as Record<string, unknown>)[fieldKey];
    if (fs.input === "enum") {
      return (
        <Field key={fieldKey} label={fs.label}>
          <Combobox value={(current as string) ?? fs.options?.[0] ?? ""}
            onChange={(v) => set({ [fieldKey]: v } as Partial<Cfg>)} options={fs.options ?? []} allowCustom={false} />
        </Field>
      );
    }
    if (fs.input === "number") {
      return (
        <Field key={fieldKey} label={fs.label} hint={fs.help}>
          <TextInput type="number" value={current == null ? "" : String(current)}
            onChange={(e) => set({ [fieldKey]: e.target.value === "" ? null : Number(e.target.value) } as Partial<Cfg>)}
            placeholder="e.g. 86400" />
        </Field>
      );
    }
    if (fs.input === "bool") {
      return (
        <Field key={fieldKey} label={fs.label} hint={fs.help}>
          <label className="inline-flex items-center gap-2 text-[13px] text-slate-600">
            <input type="checkbox" checked={current === true}
              onChange={(e) => set({ [fieldKey]: e.target.checked } as Partial<Cfg>)} />
            <span>enabled</span>
          </label>
        </Field>
      );
    }
    if (fs.input === "text") {
      return (
        <Field key={fieldKey} label={fs.label} hint={fs.help}>
          <TextInput value={(current as string) ?? ""}
            onChange={(e) => set({ [fieldKey]: e.target.value } as Partial<Cfg>)} placeholder="e.g. 9999-12-31" />
        </Field>
      );
    }
    if (fs.input === "columns") {
      const vals = Array.isArray(current) ? (current as string[]) : [];
      return (
        <Field key={fieldKey} label={fs.label} hint={fs.help ?? "one or more columns, comma-separated"}>
          <TextInput value={vals.join(", ")}
            onChange={(e) => set({ [fieldKey]: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) } as Partial<Cfg>)}
            placeholder="e.g. CUSTOMER_ID" />
        </Field>
      );
    }
    // column → typed name shared across the selected tables
    return (
      <Field key={fieldKey} label={fs.label} hint="column name present in the selected tables">
        <TextInput value={(current as string) ?? ""}
          onChange={(e) => set({ [fieldKey]: e.target.value } as Partial<Cfg>)} placeholder="e.g. BATCH_ID" />
      </Field>
    );
  };

  return (
    <div className="rounded-lg border-2 border-dashed border-teal/40 bg-teal/[0.04] p-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm font-semibold text-navy">
          Bulk apply <span className="font-normal text-slate-400">· {count} of {total} selected</span>
        </div>
        <div className="flex gap-2 text-xs">
          <button onClick={onSelectAll} className="font-medium text-teal-700 hover:underline">select all</button>
          <button onClick={onSelectNone} className="text-slate-400 hover:text-slate-600">clear</button>
        </div>
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Load pattern">
          <Combobox value={value.load_pattern} onChange={(v) => set({ load_pattern: v })} allowCustom={false}
            options={specs.patterns.map((p) => ({ value: p.pattern, label: p.label }))} />
        </Field>
        {spec && spec.fields.map(renderField)}
      </div>
      {spec?.description && <p className="mt-1 text-xs text-slate-400">{spec.description}</p>}
      <div className="mt-3">
        <Button onClick={onApply} disabled={count === 0 || value.load_pattern === "unknown"}>
          Apply to {count} selected table{count === 1 ? "" : "s"}
        </Button>
      </div>
    </div>
  );
}
