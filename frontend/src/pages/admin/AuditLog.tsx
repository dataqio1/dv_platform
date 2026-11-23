import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type AuditQuery } from "../../api";
import type { AuditEntry } from "../../types";
import { Button, Card, EmptyState, Field, Spinner, TextInput } from "../../components/ui";
import { SortTh, useSort } from "../../components/sortable";
import { fmtDateTime } from "../../tz";

const compact = (d: Record<string, unknown>) =>
  Object.entries(d || {})
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
    .join(" · ");

// A datetime-local value (local wall-clock) → ISO-8601 the API filters on.
const toIso = (local: string) => (local ? new Date(local).toISOString() : undefined);

// datetime-local string for N hours ago (default window — avoids scanning the whole trail).
const hoursAgoLocal = (h: number) => {
  const d = new Date(Date.now() - h * 3600 * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
};

export default function AuditLog() {
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [q, setQ] = useState("");
  const [action, setAction] = useState("");
  const [since, setSince] = useState(hoursAgoLocal(24)); // last 24h by default
  const [until, setUntil] = useState("");
  const [err, setErr] = useState<string | null>(null);

  // Server-side filters (action + time window) build the query; the text box filters client-side.
  const query = useMemo<AuditQuery>(
    () => ({ action: action.trim() || undefined, since: toIso(since), until: toIso(until), limit: 200 }),
    [action, since, until],
  );

  const load = useCallback(() => {
    setEntries(null);
    setErr(null);
    api.auditLog(query).then((r) => setEntries(r.data)).catch((e) => setErr((e as Error).message));
  }, [query]);
  useEffect(() => {
    load();
  }, [load]);

  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!entries) return [];
    if (!s) return entries;
    return entries.filter((e) =>
      [e.action, e.entity_type, e.entity_id, e.actor, e.actor_email, compact(e.detail)].some((v) =>
        (v || "").toLowerCase().includes(s),
      ),
    );
  }, [entries, q]);

  const { sorted, sort, requestSort } = useSort(filtered, {
    when: (e) => e.ts,
    actor: (e) => e.actor || e.actor_email || e.user_id,
    action: (e) => e.action,
    entity: (e) => e.entity_type,
  });

  const dateCls =
    "w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal focus:ring-2 focus:ring-teal/20";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold text-navy">Audit Log</h2>
          <p className="text-sm text-slate-500">Append-only record of changes in this group. Showing the last 24 hours by default — widen the From/To window for more.</p>
        </div>
        <a href={api.auditExportUrl(query)} download>
          <Button variant="ghost">Export CSV</Button>
        </a>
      </div>

      <Card className="grid grid-cols-1 gap-3 p-4 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="Action" hint="exact, e.g. incident.silence">
          <TextInput value={action} onChange={(e) => setAction(e.target.value)} placeholder="any action" />
        </Field>
        <Field label="From">
          <input type="datetime-local" className={dateCls} value={since} onChange={(e) => setSince(e.target.value)} />
        </Field>
        <Field label="To">
          <input type="datetime-local" className={dateCls} value={until} onChange={(e) => setUntil(e.target.value)} />
        </Field>
        <Field label="Find in results">
          <TextInput value={q} onChange={(e) => setQ(e.target.value)} placeholder="actor, entity, detail…" />
        </Field>
      </Card>

      {entries === null ? (
        err ? <p className="text-sm text-rose-600">{err}</p> : <Spinner />
      ) : filtered.length === 0 ? (
        <EmptyState title="No matching activity" hint="Adjust the filters above." />
      ) : (
        <Card className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                <SortTh label="When" columnKey="when" sort={sort} onSort={requestSort} className="px-4 py-2 font-semibold" />
                <SortTh label="Actor" columnKey="actor" sort={sort} onSort={requestSort} className="px-4 py-2 font-semibold" />
                <SortTh label="Action" columnKey="action" sort={sort} onSort={requestSort} className="px-4 py-2 font-semibold" />
                <SortTh label="Entity" columnKey="entity" sort={sort} onSort={requestSort} className="px-4 py-2 font-semibold" />
                <SortTh label="Detail" sortable={false} sort={sort} onSort={requestSort} className="px-4 py-2 font-semibold" />
              </tr>
            </thead>
            <tbody>
              {sorted.map((e) => (
                <tr key={e.id} className="border-b border-slate-100 align-top last:border-0">
                  <td className="whitespace-nowrap px-4 py-2 text-slate-500">
                    {fmtDateTime(e.ts)}
                  </td>
                  <td className="px-4 py-2 text-slate-700">
                    {e.actor || e.actor_email || (e.user_id ? e.user_id.slice(0, 8) : "system")}
                  </td>
                  <td className="px-4 py-2">
                    <span className="rounded bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-700">{e.action}</span>
                  </td>
                  <td className="px-4 py-2 text-slate-500">
                    {e.entity_type ?? "—"}
                    {e.entity_id && <span className="text-slate-400"> · {e.entity_id.slice(0, 8)}</span>}
                  </td>
                  <td className="px-4 py-2 font-mono text-xs text-slate-500">{compact(e.detail)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
