import { useEffect, useState } from "react";
import { api } from "../../api";
import type { PoolStats } from "../../types";
import { Button, Card, Field, Spinner, TextInput } from "../../components/ui";

/** Admin → Compute: the worker pool the scheduler packs jobs into. Set total memory + vCPUs
 *  (blank = auto-detect from the host); each test case's memory budget is charged against this,
 *  so a job waits in the queue until enough memory + vCPUs are free. */
export default function Compute() {
  const [pool, setPool] = useState<PoolStats | null>(null);
  const [mem, setMem] = useState("");
  const [cpu, setCpu] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const load = () => api.getCompute().then((r) => {
    setPool(r.data.pool ?? null);
    setMem(r.data.value?.total_memory_gb ? String(r.data.value.total_memory_gb) : "");
    setCpu(r.data.value?.max_vcpus ? String(r.data.value.max_vcpus) : "");
  }).finally(() => setLoading(false));

  useEffect(() => { load(); }, []);
  // Keep the live usage fresh while the page is open.
  useEffect(() => {
    const id = setInterval(() => api.getCompute().then((r) => setPool(r.data.pool ?? null)).catch(() => undefined), 3000);
    return () => clearInterval(id);
  }, []);

  const save = async () => {
    setSaving(true); setSaved(false);
    try {
      const r = await api.setCompute({
        total_memory_gb: mem ? Number(mem) : null,
        max_vcpus: cpu ? Number(cpu) : null,
      });
      setPool(r.data.pool ?? null);
      setSaved(true);
    } finally { setSaving(false); }
  };

  if (loading) return <Spinner />;

  const bar = (used: number, total: number) => {
    const pct = total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0;
    return (
      <div className="h-2 overflow-hidden rounded-full bg-slate-100">
        <div className={`h-full ${pct > 85 ? "bg-rose-500" : pct > 60 ? "bg-amber-500" : "bg-teal"}`} style={{ width: `${pct}%` }} />
      </div>
    );
  };

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-bold text-navy">Compute</h1>
        <p className="mt-1 text-sm text-slate-500">
          The worker pool test-case runs are scheduled into. A job is admitted only when enough
          memory and vCPUs are free — otherwise it waits in the queue. Each test case's memory
          budget (Configure &amp; run step) is what gets charged here.
        </p>
      </div>

      {pool && (
        <Card className="space-y-4 p-5">
          <div className="text-sm font-semibold text-navy">Live pool</div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1">
              <div className="flex items-center justify-between text-xs text-slate-500">
                <span>Memory</span>
                <span><span className="font-semibold text-navy">{pool.used_memory_gb}</span> / {pool.total_memory_gb} GB in use</span>
              </div>
              {bar(pool.used_memory_gb, pool.total_memory_gb)}
            </div>
            <div className="space-y-1">
              <div className="flex items-center justify-between text-xs text-slate-500">
                <span>vCPUs</span>
                <span><span className="font-semibold text-navy">{pool.used_vcpus}</span> / {pool.total_vcpus} in use</span>
              </div>
              {bar(pool.used_vcpus, pool.total_vcpus)}
            </div>
          </div>
          {pool.waiting_for_resources > 0 && (
            <p className="text-xs text-amber-600">⏳ {pool.waiting_for_resources} job{pool.waiting_for_resources === 1 ? "" : "s"} waiting for resources to free up.</p>
          )}
        </Card>
      )}

      <Card className="space-y-4 p-5">
        <div className="text-sm font-semibold text-navy">Limits</div>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Total memory (GB)" hint="Blank = auto-detect from the host. Applied immediately.">
            <TextInput type="number" value={mem} onChange={(e) => { setMem(e.target.value); setSaved(false); }}
              placeholder={pool ? `auto (${pool.total_memory_gb})` : "auto"} />
          </Field>
          <Field label="Max vCPUs" hint="Blank = auto-detect (host CPU count).">
            <TextInput type="number" value={cpu} onChange={(e) => { setCpu(e.target.value); setSaved(false); }}
              placeholder={pool ? `auto (${pool.total_vcpus})` : "auto"} />
          </Field>
        </div>
        <div className="flex items-center gap-3">
          <Button onClick={save} disabled={saving}>{saving ? "Saving…" : "Save limits"}</Button>
          {saved && <span className="text-xs font-medium text-emerald-600">✓ Applied to the live scheduler</span>}
        </div>
      </Card>
    </div>
  );
}
