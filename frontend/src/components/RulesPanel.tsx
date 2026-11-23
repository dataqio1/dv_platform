import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { Health, Rule, RulePoint } from "../types";
import { Button, Card, Field, HealthBadge, Spinner, TextInput } from "./ui";
import { TrendChart } from "./TrendChart";

const RULE_TYPES = [
  "custom_sql",
  "metric",
  "not_null",
  "null_rate",
  "unique",
  "range",
  "regex",
  "freshness",
  "row_count",
];

const SEVERITIES = ["info", "warning", "critical"];

// Provenance label — who/what authored the rule (docs/design/11 §6.2).
const ORIGIN_CHIP: Record<string, { label: string; cls: string }> = {
  system: { label: "Automated", cls: "bg-teal/10 text-teal-700" },
  suggested: { label: "Suggested", cls: "bg-violet-50 text-violet-600" },
  user: { label: "User", cls: "bg-slate-100 text-slate-500" },
  injected: { label: "Injected", cls: "bg-indigo-50 text-indigo-600" },
};

function OriginChip({ origin }: { origin?: string }) {
  const o = ORIGIN_CHIP[origin ?? "user"] ?? ORIGIN_CHIP.user;
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${o.cls}`} title={`Rule origin: ${origin ?? "user"}`}>
      {o.label}
    </span>
  );
}

export function FailingKeys({
  keys,
  sql,
}: {
  keys: Record<string, unknown>[];
  sql?: string | null;
}) {
  const cols = Object.keys(keys[0] ?? {});
  return (
    <div className="mb-3">
      <div className="mb-1 text-xs font-medium text-slate-500">
        Failing keys ({keys.length})
      </div>
      <div className="overflow-x-auto rounded border border-slate-200">
        <table className="w-full text-xs">
          <thead>
            <tr>
              {cols.map((col) => (
                <th key={col} className="px-2 py-1 text-left font-medium text-slate-400">
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {keys.map((row, i) => (
              <tr key={i} className="border-t border-slate-100">
                {cols.map((col) => (
                  <td key={col} className="px-2 py-1 font-mono text-slate-700">
                    {row[col] == null ? "null" : String(row[col])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {sql && (
        <pre className="mt-2 overflow-x-auto rounded bg-slate-900 p-3 text-xs text-slate-100">{sql}</pre>
      )}
    </div>
  );
}

function ruleHealth(rule: Rule): Health {
  if (!rule.latest) return "unknown";
  return rule.latest.passed ? "healthy" : "critical";
}

function RuleRow({
  rule,
  monitorId,
  tableRef,
  onChanged,
}: {
  rule: Rule;
  monitorId: string;
  tableRef: string;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [points, setPoints] = useState<RulePoint[] | null>(null);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);

  const loadHistory = useCallback(() => {
    api.ruleHistory(monitorId, rule.id).then((r) => setPoints(r.data.points));
  }, [monitorId, rule.id]);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && points === null) loadHistory();
  };

  const record = async () => {
    if (value === "") return;
    setBusy(true);
    try {
      await api.recordRuleResult(monitorId, rule.id, {
        table_ref: tableRef,
        metric_value: Number(value),
      });
      setValue("");
      loadHistory();
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  const evaluate = async () => {
    setBusy(true);
    try {
      await api.evaluateRule(monitorId, rule.id);
      if (!open) setOpen(true);
      loadHistory();
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  const latest = rule.latest;
  return (
    <div className="rounded-lg border border-slate-100">
      <button onClick={toggle} className="flex w-full items-center gap-3 px-4 py-3 text-left">
        <HealthBadge status={ruleHealth(rule)} label={latest ? (latest.passed ? "Pass" : "Fail") : "No data"} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-medium text-navy">{rule.name}</span>
            <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-slate-500">
              {rule.type}
            </span>
            <span className="text-[10px] uppercase tracking-wide text-slate-400">{rule.severity}</span>
            <OriginChip origin={rule.origin} />
            {rule.state === "suggested" && (
              <span className="rounded bg-violet-50 px-1.5 py-0.5 text-[10px] font-medium text-violet-600">Suggested</span>
            )}
            {rule.state === "disabled" && (
              <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-400">Disabled</span>
            )}
          </div>
          {rule.description && <div className="truncate text-xs text-slate-400">{rule.description}</div>}
        </div>
        <div className="text-right text-xs text-slate-500">
          {latest ? (
            <>
              <span className="font-semibold text-navy">{latest.metric_value ?? "—"}</span>
              {latest.threshold != null && <span className="text-slate-400"> / thr {latest.threshold}</span>}
            </>
          ) : (
            <span className="text-slate-300">no result</span>
          )}
        </div>
        <span className="text-slate-300">{open ? "▾" : "▸"}</span>
      </button>

      {open && (
        <div className="border-t border-slate-100 p-4">
          {(rule.config?.expression as string) && (
            <pre className="mb-3 overflow-x-auto rounded bg-slate-900 p-3 text-xs text-slate-100">
              {String(rule.config.expression)}
            </pre>
          )}
          {latest && !latest.passed && (latest.failing_keys?.length ?? 0) > 0 && (
            <FailingKeys keys={latest.failing_keys ?? []} sql={latest.sample_sql} />
          )}
          {points === null ? (
            <Spinner />
          ) : (
            <TrendChart
              points={points
                .filter((p) => p.value != null)
                .map((p) => ({ ts: p.ts, value: p.value as number }))}
              color="#0ea5e9"
              height={160}
            />
          )}
          <div className="mt-3 flex items-end gap-3">
            <Button onClick={evaluate} disabled={busy}>
              {busy ? "Evaluating…" : "Evaluate now"}
            </Button>
            <span className="pb-2 text-xs text-slate-300">or</span>
            <div className="w-36">
              <Field label="Record a value" hint="manual override">
                <TextInput
                  type="number"
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                  placeholder="e.g. 0"
                />
              </Field>
            </div>
            <Button variant="ghost" onClick={record} disabled={busy || value === ""}>
              Record
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

export function RulesPanel({
  monitorId,
  tableRef,
  refreshSignal = 0,
}: {
  monitorId: string;
  tableRef: string;
  refreshSignal?: number;
}) {
  const [rules, setRules] = useState<Rule[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [genOpen, setGenOpen] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // add-rule form state
  const [name, setName] = useState("");
  const [type, setType] = useState("custom_sql");
  const [severity, setSeverity] = useState("warning");
  const [columns, setColumns] = useState("");
  const [threshold, setThreshold] = useState("");
  const [expression, setExpression] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(() => {
    api
      .tableRules(monitorId, tableRef)
      .then((r) => setRules(r.data))
      .catch((e) => setErr(e.message));
  }, [monitorId, tableRef]);

  useEffect(() => {
    load();
  }, [load, refreshSignal]);

  const submit = async () => {
    setSaving(true);
    setErr(null);
    try {
      const config: Record<string, unknown> = { dimension: "quality" };
      if (threshold !== "") config.threshold = Number(threshold);
      if (expression) config.expression = expression;
      await api.createRule(monitorId, {
        name,
        type,
        severity,
        table_ref: tableRef,
        columns: columns
          .split(",")
          .map((c) => c.trim())
          .filter(Boolean),
        config,
      });
      setName("");
      setColumns("");
      setThreshold("");
      setExpression("");
      setAdding(false);
      load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const showExpr = type === "custom_sql" || type === "metric";

  return (
    <Card className="p-5">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-navy">Data Quality Rules</h3>
        <div className="flex items-center gap-2">
          <Button variant="ghost" onClick={() => { setGenOpen(!genOpen); setAdding(false); }}>
            {genOpen ? "Close" : "✨ Generate from sample"}
          </Button>
          <Button variant={adding ? "ghost" : "primary"} onClick={() => { setAdding(!adding); setGenOpen(false); }}>
            {adding ? "Close" : "+ Add rule"}
          </Button>
        </div>
      </div>

      {genOpen && (
        <GenerateFromSample monitorId={monitorId} tableRef={tableRef}
          onAdded={() => { setGenOpen(false); load(); }} />
      )}

      {adding && (
        <div className="mb-4 space-y-3 rounded-lg bg-slate-50 p-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Name">
              <TextInput value={name} onChange={(e) => setName(e.target.value)} placeholder="no_negative_amount" />
            </Field>
            <Field label="Type">
              <select
                value={type}
                onChange={(e) => setType(e.target.value)}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal"
              >
                {RULE_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          {showExpr && (
            <Field label="SQL expression" hint="Use {{table}} as the table placeholder; metric = the value it returns">
              <textarea
                value={expression}
                onChange={(e) => setExpression(e.target.value)}
                rows={2}
                placeholder="SELECT COUNT(*) AS v FROM {{table}} WHERE AMOUNT < 0"
                className="w-full rounded-lg border border-slate-300 px-3 py-2 font-mono text-xs outline-none focus:border-teal"
              />
            </Field>
          )}
          <div className="grid grid-cols-3 gap-3">
            <Field label="Columns" hint="optional, comma-sep">
              <TextInput value={columns} onChange={(e) => setColumns(e.target.value)} placeholder="AMOUNT" />
            </Field>
            <Field label="Threshold" hint="pass when value ≤ this">
              <TextInput type="number" value={threshold} onChange={(e) => setThreshold(e.target.value)} placeholder="0" />
            </Field>
            <Field label="Severity">
              <select
                value={severity}
                onChange={(e) => setSeverity(e.target.value)}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal"
              >
                {SEVERITIES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <div className="flex justify-end">
            <Button onClick={submit} disabled={!name || saving}>
              {saving ? "Saving…" : "Create rule"}
            </Button>
          </div>
        </div>
      )}

      {err && <p className="mb-2 text-xs text-rose-600">{err}</p>}

      {rules === null ? (
        <Spinner />
      ) : rules.length === 0 ? (
        <div className="text-xs text-slate-400">
          No rules yet. Add a custom data quality rule (e.g. “AMOUNT must never be negative”) and its
          metric will chart over time.
        </div>
      ) : (
        <div className="space-y-2">
          {rules.map((r) => (
            <RuleRow key={r.id} rule={r} monitorId={monitorId} tableRef={tableRef} onChanged={load} />
          ))}
        </div>
      )}
    </Card>
  );
}

// ── Generate DQ rules from a sample (latest watermark batch, else ~N rows), review, dry-run, add ──
type Spec = { name: string; type: string; columns?: string[]; config?: Record<string, unknown>; severity?: string; confidence?: number };
type DryResult = { name: string; applicable: boolean; passed?: boolean; measured?: unknown; note?: string; failing?: Record<string, unknown>[] };

const LEVELS: { v: number; label: string; hint: string }[] = [
  { v: 1, label: "Essential", hint: "row count, not-null, freshness" },
  { v: 2, label: "Standard", hint: "+ null-rate, uniqueness, numeric ranges" },
  { v: 3, label: "Comprehensive", hint: "+ pattern rules (email, …)" },
];

function GenerateFromSample({ monitorId, tableRef, onAdded }: {
  monitorId: string; tableRef: string; onAdded: () => void;
}) {
  const [level, setLevel] = useState(2);
  const [sampleSize, setSampleSize] = useState(10000);
  const [sug, setSug] = useState<{ sampled: number; watermark_used: boolean; rules: Spec[] } | null>(null);
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [dry, setDry] = useState<Record<string, DryResult> | null>(null);

  const selectedRules = (): Spec[] => (sug?.rules ?? []).filter((_, i) => sel.has(i));

  const generate = async () => {
    setLoading(true); setErr(null); setSug(null); setDry(null);
    try {
      const r = await api.suggestRulesFromSample(monitorId, { level, sample_size: sampleSize });
      setSug({ sampled: r.data.sampled, watermark_used: r.data.watermark_used, rules: r.data.rules as Spec[] });
      setSel(new Set((r.data.rules as Spec[]).map((_, i) => i)));
    } catch (e) { setErr((e as Error).message); } finally { setLoading(false); }
  };
  const test = async () => {
    if (!sug) return;
    setBusy(true); setErr(null);
    try {
      const r = await api.dryRunRules(monitorId, { rules: selectedRules(), sample_size: sampleSize });
      const by: Record<string, DryResult> = {};
      for (const res of r.data.results) by[res.name] = res;
      setDry(by);
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };
  const addSelected = async () => {
    setBusy(true); setErr(null);
    try {
      for (const spec of selectedRules()) {
        await api.createRule(monitorId, {
          name: spec.name, type: spec.type, severity: spec.severity ?? "warning",
          table_ref: tableRef, columns: spec.columns ?? [], config: spec.config ?? {},
        });
      }
      onAdded();
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };

  return (
    <div className="mb-4 space-y-3 rounded-lg border border-violet-200 bg-violet-50/40 p-4">
      <div className="text-sm font-semibold text-navy">Generate rules from a data sample</div>
      <p className="text-xs text-slate-500">
        Profiles the latest batch (by watermark) or ~N rows and proposes rules at the detail level you pick.
        Nothing is saved until you review, test, and add them. Works for tables and files on S3.
      </p>
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Detail level">
          <select value={level} onChange={(e) => setLevel(Number(e.target.value))}
            className="w-56 rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal">
            {LEVELS.map((l) => <option key={l.v} value={l.v}>{l.v} · {l.label} — {l.hint}</option>)}
          </select>
        </Field>
        <Field label="Sample size (no watermark)">
          <TextInput type="number" value={sampleSize} onChange={(e) => setSampleSize(Number(e.target.value) || 0)} className="w-40" />
        </Field>
        <Button onClick={generate} disabled={loading}>{loading ? "Profiling…" : "Generate"}</Button>
      </div>
      {err && <p className="text-xs text-rose-600">{err}</p>}

      {loading && (
        <div className="flex flex-col items-center gap-2 py-6 text-center">
          <span className="h-6 w-6 animate-spin rounded-full border-2 border-slate-300 border-t-violet-500" />
          <div className="text-xs font-semibold text-navy">Profiling the sample…</div>
        </div>
      )}

      {sug && (
        <div className="space-y-2">
          <div className="flex items-center justify-between text-xs text-slate-500">
            <span>{sug.rules.length} rule{sug.rules.length === 1 ? "" : "s"} proposed from {sug.sampled.toLocaleString()} rows{sug.watermark_used ? " (latest watermark batch)" : ""} · {sel.size} selected</span>
            <span className="flex gap-2">
              <button onClick={() => setSel(new Set(sug.rules.map((_, i) => i)))} className="text-teal-700 hover:underline">select all</button>
              <button onClick={() => setSel(new Set())} className="text-slate-400 hover:text-slate-600">none</button>
            </span>
          </div>
          <div className="max-h-80 overflow-auto rounded-lg border border-slate-200 bg-white">
            {sug.rules.map((r, i) => {
              const d = dry?.[r.name];
              return (
                <label key={i} className="flex cursor-pointer items-center gap-2 border-b border-slate-50 px-2 py-1.5 text-[11px] last:border-0 hover:bg-slate-50">
                  <input type="checkbox" checked={sel.has(i)}
                    onChange={(e) => setSel((s) => { const n = new Set(s); if (e.target.checked) n.add(i); else n.delete(i); return n; })} />
                  <span className="w-44 shrink-0 truncate font-mono font-medium text-navy" title={r.name}>{r.name}</span>
                  <span className="w-24 shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-center text-[10px] text-slate-500">{r.type}</span>
                  <span className="flex-1 truncate text-slate-400">{(r.columns ?? []).join(", ")} {r.config && Object.keys(r.config).length ? `· ${JSON.stringify(r.config)}` : ""}</span>
                  {d && (d.applicable === false
                    ? <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-400" title={d.note}>n/a on sample</span>
                    : d.passed
                      ? <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">✓ pass</span>
                      : <span className="rounded bg-rose-50 px-1.5 py-0.5 text-[10px] font-medium text-rose-700" title={`${d.measured} violation(s) on the sample`}>✗ {String(d.measured)} fail</span>)}
                </label>
              );
            })}
          </div>
          <div className="flex items-center justify-end gap-2">
            <Button variant="ghost" onClick={test} disabled={busy || sel.size === 0}>{busy ? "Testing…" : "🧪 Test on sample"}</Button>
            <Button onClick={addSelected} disabled={busy || sel.size === 0}>Add {sel.size} selected rule{sel.size === 1 ? "" : "s"}</Button>
          </div>
        </div>
      )}
    </div>
  );
}
