import { useEffect, useState } from "react";
import { api } from "../api";
import type { RuleSuggestion } from "../types";
import { Button, Card, TextInput } from "./ui";

/**
 * Load-pattern rule suggestions. When a table's load pattern is known, we recommend baseline
 * rules (e.g. "row count within ±5% of recent average" for a truncate-and-load table). The user
 * ticks the ones they want, tweaks the numbers, and acknowledges — which creates a row-count
 * metric + the chosen rules on the monitor. Nothing is applied until the user acknowledges.
 */
export function SuggestedRules({
  monitorId,
  connectionId,
  tableRef,
  loadPattern,
  onAdded,
}: {
  monitorId: string;
  connectionId: string;
  tableRef: string;
  loadPattern: string;
  onAdded?: () => void;
}) {
  const [suggestions, setSuggestions] = useState<RuleSuggestion[] | null>(null);
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [params, setParams] = useState<Record<string, Record<string, number>>>({});
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!loadPattern || loadPattern === "unknown") {
      setSuggestions([]);
      return;
    }
    setDone(null);
    api
      .ruleSuggestions(loadPattern)
      .then((r) => {
        setSuggestions(r.data);
        // default-tick all, prefill param defaults
        const pick: Record<string, boolean> = {};
        const pars: Record<string, Record<string, number>> = {};
        for (const s of r.data) {
          pick[s.key] = true;
          pars[s.key] = Object.fromEntries(s.params.map((p) => [p.key, p.default]));
        }
        setPicked(pick);
        setParams(pars);
      })
      .catch((e) => setErr((e as Error).message));
  }, [loadPattern]);

  if (!suggestions || suggestions.length === 0) return null;

  const setParam = (sKey: string, pKey: string, value: number) =>
    setParams((prev) => ({ ...prev, [sKey]: { ...prev[sKey], [pKey]: value } }));

  const acknowledge = async () => {
    const selections = suggestions
      .filter((s) => picked[s.key])
      .map((s) => ({ key: s.key, params: params[s.key] ?? {} }));
    if (selections.length === 0) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await api.acknowledgeTableSuggestions({
        monitor_id: monitorId,
        table_ref: tableRef,
        connection_id: connectionId,
        load_pattern: loadPattern,
        selections,
      });
      setDone(`Added ${r.data.rules.length} rule${r.data.rules.length === 1 ? "" : "s"} on "${r.data.metric.name}".`);
      onAdded?.();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const chosenCount = suggestions.filter((s) => picked[s.key]).length;

  return (
    <Card className="space-y-3 border-teal/30 bg-teal/5 p-4">
      <div>
        <h3 className="text-sm font-semibold text-navy">Suggested rules for this load pattern</h3>
        <p className="mt-0.5 text-xs text-slate-500">
          Based on how <span className="font-mono">{tableRef.split(".").slice(-1)[0]}</span> is
          loaded. Tick the ones you want, adjust the numbers, then add them.
        </p>
      </div>

      {done ? (
        <p className="text-sm text-emerald-700">✓ {done}</p>
      ) : (
        <>
          <div className="space-y-2">
            {suggestions.map((s) => (
              <label key={s.key} className="flex items-start gap-2 rounded-lg bg-white/70 p-2.5">
                <input
                  type="checkbox"
                  checked={!!picked[s.key]}
                  onChange={(e) => setPicked((p) => ({ ...p, [s.key]: e.target.checked }))}
                  className="mt-0.5 accent-teal"
                />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-navy">{s.label}</span>
                    <span
                      className={`rounded-full px-1.5 py-0.5 text-[10px] font-semibold uppercase ${
                        s.severity === "fail" ? "bg-rose-100 text-rose-700" : "bg-amber-100 text-amber-700"
                      }`}
                    >
                      {s.severity}
                    </span>
                  </div>
                  <p className="mt-0.5 text-xs text-slate-500">{s.rationale}</p>
                  {s.params.length > 0 && picked[s.key] && (
                    <div className="mt-2 flex flex-wrap gap-3">
                      {s.params.map((p) => (
                        <label key={p.key} className="flex items-center gap-1.5 text-xs text-slate-600">
                          {p.label}
                          <TextInput
                            type="number"
                            value={String(params[s.key]?.[p.key] ?? p.default)}
                            onChange={(e) => setParam(s.key, p.key, Number(e.target.value))}
                            className="w-24 py-1"
                          />
                        </label>
                      ))}
                    </div>
                  )}
                </div>
              </label>
            ))}
          </div>
          {err && <p className="text-xs text-rose-600">{err}</p>}
          <div className="flex items-center gap-3">
            <Button onClick={acknowledge} disabled={busy || chosenCount === 0}>
              {busy ? "Adding…" : `Add ${chosenCount} rule${chosenCount === 1 ? "" : "s"}`}
            </Button>
            <span className="text-xs text-slate-400">Creates a row-count metric + these rules.</span>
          </div>
        </>
      )}
    </Card>
  );
}
