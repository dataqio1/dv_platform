import { useEffect, useRef, useState } from "react";

type Opt = { value: string; label: string };
const norm = (options: (string | Opt)[]): Opt[] =>
  options.map((o) => (typeof o === "string" ? { value: o, label: o } : o));

/** Type-to-filter dropdown. Pick from the list OR type a free value (allowCustom).
 *  Scales to thousands of options (filtered, capped render). */
export function Combobox({
  value,
  onChange,
  options,
  placeholder,
  allowCustom = true,
  disabled,
  loading,
}: {
  value: string;
  onChange: (v: string) => void;
  options: (string | Opt)[];
  placeholder?: string;
  allowCustom?: boolean;
  disabled?: boolean;
  loading?: boolean;
}) {
  const opts = norm(options);
  const labelOf = (v: string) => opts.find((o) => o.value === v)?.label ?? v;
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState(labelOf(value));
  const [hi, setHi] = useState(0);
  const boxRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // keep the input text in sync with the selected value when closed
  useEffect(() => {
    if (!open) setQuery(labelOf(value));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, options.length]);

  const unchanged = query === labelOf(value);
  const filtered = unchanged
    ? opts
    : opts.filter((o) => o.label.toLowerCase().includes(query.toLowerCase()));

  const commit = () => {
    setOpen(false);
    if (allowCustom && query !== labelOf(value)) onChange(query);
    else setQuery(labelOf(value));
  };

  useEffect(() => {
    const h = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) commit();
    };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  });

  const pick = (o: Opt) => {
    onChange(o.value);
    setQuery(o.label);
    setOpen(false);
  };

  return (
    <div ref={boxRef} className="relative">
      <input
        ref={inputRef}
        value={query}
        disabled={disabled}
        placeholder={placeholder}
        onFocus={() => {
          setOpen(true);
          setHi(0);
          inputRef.current?.select();
        }}
        onChange={(e) => {
          setQuery(e.target.value);
          setOpen(true);
          setHi(0);
        }}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            setHi((h) => Math.min(h + 1, filtered.length - 1));
            e.preventDefault();
          } else if (e.key === "ArrowUp") {
            setHi((h) => Math.max(h - 1, 0));
            e.preventDefault();
          } else if (e.key === "Enter") {
            if (filtered[hi]) pick(filtered[hi]);
            else commit();
            e.preventDefault();
          } else if (e.key === "Escape") {
            setOpen(false);
            setQuery(labelOf(value));
          }
        }}
        className="w-full rounded-lg border border-slate-300 px-3 py-2 pr-8 text-sm outline-none focus:border-teal disabled:bg-slate-50"
      />
      {/* Dropdown affordance — makes it obvious you can pick from a list, not just type. */}
      <button
        type="button"
        tabIndex={-1}
        disabled={disabled}
        aria-label="show options"
        onMouseDown={(e) => {
          e.preventDefault();
          if (open) {
            setOpen(false);
          } else {
            inputRef.current?.focus();
            setQuery(labelOf(value)); // show the full list, not a stale filter
            setOpen(true);
            setHi(0);
          }
        }}
        className="absolute inset-y-0 right-0 flex items-center px-2 text-slate-400 hover:text-slate-600 disabled:opacity-40"
      >
        <svg width="14" height="14" viewBox="0 0 20 20" fill="none" aria-hidden="true">
          <path d="M6 8l4 4 4-4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open && (
        <div className="absolute z-30 mt-1 max-h-60 w-full overflow-auto rounded-lg border border-slate-200 bg-white shadow-lg">
          {loading ? (
            <div className="px-3 py-2 text-xs text-slate-400">Loading…</div>
          ) : filtered.length === 0 ? (
            <div className="px-3 py-2 text-xs text-slate-400">
              {opts.length === 0
                ? allowCustom ? "No options — type a value" : "No options"
                : allowCustom ? `No match — press Enter to use “${query}”` : "No match"}
            </div>
          ) : (
            <>
              {filtered.slice(0, 200).map((o, i) => (
                <button
                  key={o.value}
                  type="button"
                  onMouseDown={(e) => {
                    e.preventDefault();
                    pick(o);
                  }}
                  className={`block w-full px-3 py-1.5 text-left text-sm ${
                    i === hi ? "bg-teal/10 text-teal-700" : "hover:bg-slate-50"
                  }`}
                >
                  {o.label}
                </button>
              ))}
              {filtered.length > 200 && (
                <div className="px-3 py-1 text-xs text-slate-400">
                  +{filtered.length - 200} more — keep typing to narrow
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

/** Table/view picker for large catalogs (200+). Filter by type (tables / views / both),
 *  search, select-all-matching, per-item include/exclude chips, type-to-add, live counts. */
export function TablePicker({
  items,
  value,
  onChange,
}: {
  items: { name: string; type: string }[];
  value: string[];
  onChange: (v: string[]) => void;
}) {
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState<"all" | "table" | "view">("all");
  const sel = new Set(value);
  const typeOf = new Map(items.map((i) => [i.name, i.type]));

  const q = query.trim().toLowerCase();
  const filtered = items.filter(
    (i) =>
      (typeFilter === "all" || i.type === typeFilter) && (!q || i.name.toLowerCase().includes(q)),
  );
  const nTables = items.filter((i) => i.type !== "view").length;
  const nViews = items.filter((i) => i.type === "view").length;
  const selTables = value.filter((n) => typeOf.get(n) !== "view").length;
  const selViews = value.length - selTables;

  const setMany = (names: string[], on: boolean) => {
    const s = new Set(sel);
    names.forEach((n) => (on ? s.add(n) : s.delete(n)));
    onChange([...s]);
  };
  const toggle = (n: string) => setMany([n], !sel.has(n));
  const addTyped = () => {
    const n = query.trim();
    if (n && !sel.has(n)) onChange([...value, n]);
    setQuery("");
  };
  const allShownSelected = filtered.length > 0 && filtered.every((f) => sel.has(f.name));
  const seg = (v: typeof typeFilter, label: string) => (
    <button
      key={v}
      onClick={() => setTypeFilter(v)}
      className={`rounded px-2 py-0.5 text-[11px] font-medium ${
        typeFilter === v ? "bg-navy text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"
      }`}
    >
      {label}
    </button>
  );

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex gap-1">
          {seg("all", `All (${items.length})`)}
          {seg("table", `Tables (${nTables})`)}
          {seg("view", `Views (${nViews})`)}
        </div>
        <div className="flex gap-2 text-[11px]">
          <button
            onClick={() => setMany(filtered.map((f) => f.name), true)}
            className="font-medium text-teal-700 hover:underline"
          >
            Select {q || typeFilter !== "all" ? "matching" : "all"} ({filtered.length})
          </button>
          {value.length > 0 && (
            <button onClick={() => onChange([])} className="text-slate-400 hover:text-rose-500">
              Clear
            </button>
          )}
        </div>
      </div>

      {value.length > 0 && (
        <div className="flex max-h-20 flex-wrap gap-1 overflow-auto rounded-lg bg-slate-50 p-1.5">
          {value.map((v) => (
            <span
              key={v}
              className="flex items-center gap-1 rounded-full bg-teal/10 px-2 py-0.5 text-xs text-teal-700"
            >
              {v}
              {typeOf.get(v) === "view" && <span className="text-[9px] text-teal-400">view</span>}
              <button
                onClick={() => toggle(v)}
                title="exclude"
                className="text-teal-400 hover:text-rose-500"
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            addTyped();
            e.preventDefault();
          }
        }}
        placeholder="search tables & views…"
        className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal"
      />

      <div className="max-h-56 overflow-auto rounded-lg border border-slate-200">
        {filtered.length === 0 ? (
          <div className="px-3 py-2 text-xs text-slate-400">
            No match{query ? ` — press Enter to add “${query}”` : ""}.
          </div>
        ) : (
          <>
            <label className="sticky top-0 flex items-center gap-2 border-b border-slate-100 bg-white px-3 py-1.5 text-xs text-slate-500">
              <input
                type="checkbox"
                checked={allShownSelected}
                onChange={(e) => setMany(filtered.map((f) => f.name), e.target.checked)}
              />
              Select all shown ({filtered.length})
            </label>
            {filtered.slice(0, 500).map((o) => (
              <label key={o.name} className="flex items-center gap-2 px-3 py-1 text-sm hover:bg-slate-50">
                <input type="checkbox" checked={sel.has(o.name)} onChange={() => toggle(o.name)} />
                <span className="flex-1">{o.name}</span>
                {o.type === "view" && (
                  <span className="rounded bg-slate-100 px-1 text-[10px] text-slate-500">view</span>
                )}
              </label>
            ))}
            {filtered.length > 500 && (
              <div className="px-3 py-1 text-xs text-slate-400">
                +{filtered.length - 500} more — narrow your search
              </div>
            )}
          </>
        )}
      </div>

      <div className="text-[11px] text-slate-400">
        {value.length} selected ({selTables} table{selTables === 1 ? "" : "s"}, {selViews} view
        {selViews === 1 ? "" : "s"}) of {items.length} · click × on a chip to exclude
      </div>
    </div>
  );
}


/** Searchable multi-select for large lists (e.g. 3000 tables): filter, select-all-shown,
 *  chips for selected, and type-to-add a custom name. */
export function MultiSelectSearch({
  options,
  value,
  onChange,
  placeholder,
}: {
  options: string[];
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
}) {
  const [query, setQuery] = useState("");
  const sel = new Set(value);
  const filtered = query
    ? options.filter((o) => o.toLowerCase().includes(query.toLowerCase()))
    : options;

  const toggle = (n: string) => {
    const s = new Set(sel);
    s.has(n) ? s.delete(n) : s.add(n);
    onChange([...s]);
  };
  const addTyped = () => {
    const n = query.trim();
    if (n && !sel.has(n)) onChange([...value, n]);
    setQuery("");
  };

  return (
    <div className="space-y-1">
      {value.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {value.map((v) => (
            <span key={v} className="flex items-center gap-1 rounded-full bg-teal/10 px-2 py-0.5 text-xs text-teal-700">
              {v}
              <button onClick={() => toggle(v)} className="text-teal-400 hover:text-rose-500">
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            addTyped();
            e.preventDefault();
          }
        }}
        placeholder={placeholder ?? "search tables…"}
        className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal"
      />
      <div className="max-h-40 overflow-auto rounded-lg border border-slate-200">
        {filtered.length === 0 ? (
          <div className="px-3 py-2 text-xs text-slate-400">
            No match{query ? ` — press Enter to add “${query}”` : ""}.
          </div>
        ) : (
          <>
            <label className="flex items-center gap-2 border-b border-slate-100 px-3 py-1.5 text-xs text-slate-500">
              <input
                type="checkbox"
                checked={filtered.length > 0 && filtered.every((f) => sel.has(f))}
                onChange={(e) => {
                  const s = new Set(sel);
                  filtered.forEach((f) => (e.target.checked ? s.add(f) : s.delete(f)));
                  onChange([...s]);
                }}
              />
              Select all shown ({filtered.length})
            </label>
            {filtered.slice(0, 500).map((o) => (
              <label key={o} className="flex items-center gap-2 px-3 py-1 text-sm hover:bg-slate-50">
                <input type="checkbox" checked={sel.has(o)} onChange={() => toggle(o)} />
                {o}
              </label>
            ))}
            {filtered.length > 500 && (
              <div className="px-3 py-1 text-xs text-slate-400">
                +{filtered.length - 500} more — narrow your search
              </div>
            )}
          </>
        )}
      </div>
      <div className="text-[11px] text-slate-400">
        {value.length} selected · type to filter {options.length} tables, Enter to add a custom name
      </div>
    </div>
  );
}
