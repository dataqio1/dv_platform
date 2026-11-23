import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import type { Health, Incident, IncidentComment } from "../types";
import { FailingKeys } from "./RulesPanel";
import { fmtDateTime } from "../tz";
import { Button, HealthBadge, TextInput } from "./ui";

const RESOLUTION_LABEL: Record<string, string> = {
  ok: "Marked OK",
  false_positive: "False positive",
  confirmed: "Confirmed issue",
};

// Triage panel: notes (who/when) + mark an alert OK / false-positive / confirmed, or reopen.
function Triage({ incident, onChange }: { incident: Incident; onChange?: () => void }) {
  const [comments, setComments] = useState<IncidentComment[] | null>(null);
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api.incidentComments(incident.id).then((r) => setComments(r.data)).catch(() => setComments([]));
  }, [incident.id]);
  useEffect(load, [load]);

  const addComment = async () => {
    if (!body.trim()) return;
    setBusy(true);
    try {
      await api.addIncidentComment(incident.id, body.trim());
      setBody("");
      load();
    } finally {
      setBusy(false);
    }
  };
  const resolve = async (resolution: string) => {
    setBusy(true);
    try {
      await api.resolveIncident(incident.id, { resolution });
      onChange?.();
      load();
    } finally {
      setBusy(false);
    }
  };
  const reopen = async () => {
    setBusy(true);
    try {
      await api.reopenIncident(incident.id);
      onChange?.();
    } finally {
      setBusy(false);
    }
  };

  const resolved = incident.status === "resolved";
  return (
    <div className="mt-3 space-y-3 rounded-lg bg-slate-50 p-3">
      <div className="flex flex-wrap items-center gap-2">
        {resolved ? (
          <>
            <span className="text-xs text-slate-500">
              {incident.resolution ? RESOLUTION_LABEL[incident.resolution] ?? incident.resolution : "Resolved"}
            </span>
            <Button variant="ghost" className="!px-2 !py-1 text-xs" onClick={reopen} disabled={busy}>
              Reopen
            </Button>
          </>
        ) : (
          <>
            <span className="text-xs font-medium text-slate-500">Mark this alert:</span>
            <Button variant="ghost" className="!px-2 !py-1 text-xs" onClick={() => resolve("ok")} disabled={busy}>
              OK / expected
            </Button>
            <Button variant="ghost" className="!px-2 !py-1 text-xs" onClick={() => resolve("false_positive")} disabled={busy}>
              False positive
            </Button>
            <Button variant="ghost" className="!px-2 !py-1 text-xs" onClick={() => resolve("confirmed")} disabled={busy}>
              Confirm issue
            </Button>
          </>
        )}
      </div>

      <div className="space-y-1.5">
        {comments === null ? (
          <p className="text-xs text-slate-400">Loading notes…</p>
        ) : comments.length === 0 ? (
          <p className="text-xs text-slate-400">No notes yet.</p>
        ) : (
          comments.map((c) => (
            <div key={c.id} className="text-xs">
              <span className="font-medium text-slate-600">{c.author || c.author_email || "someone"}</span>
              <span className="text-slate-400"> · {fmtDateTime(c.ts)}</span>
              <div className="text-slate-700">{c.body}</div>
            </div>
          ))
        )}
      </div>
      <div className="flex gap-2">
        <TextInput
          value={body}
          onChange={(e) => setBody(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && addComment()}
          placeholder="Add a note…"
          className="!py-1 text-xs"
        />
        <Button variant="ghost" className="!px-3 !py-1 text-xs" onClick={addComment} disabled={busy || !body.trim()}>
          Add
        </Button>
      </div>
    </div>
  );
}

function sevToHealth(sev: string): Health {
  if (sev === "critical") return "critical";
  if (sev === "warning") return "warning";
  return "unknown";
}

const DURATIONS: { label: string; hours: number | "custom" }[] = [
  { label: "1 hour", hours: 1 },
  { label: "4 hours", hours: 4 },
  { label: "24 hours", hours: 24 },
  { label: "3 days", hours: 72 },
  { label: "Until a date…", hours: "custom" },
];

// Silence panel: suppress alerting for this incident (or its whole monitor) for a window,
// capturing the reason + ticket for the audit trail (Figma "Silence alert · Add notes").
function SilenceControl({ incident, onChange }: { incident: Incident; onChange?: () => void }) {
  const [scope, setScope] = useState("incident");
  const [choice, setChoice] = useState<number | "custom">(24);
  const [until, setUntil] = useState("");
  const [note, setNote] = useState("");
  const [ticket, setTicket] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const silenced = Boolean(incident.silenced);

  const silence = async () => {
    setBusy(true);
    setErr(null);
    try {
      const body =
        choice === "custom"
          ? { scope, until: until ? new Date(until).toISOString() : null, note, ticket_url: ticket }
          : { scope, duration_hours: choice, note, ticket_url: ticket };
      await api.silenceIncident(incident.id, body);
      onChange?.();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const lift = async () => {
    setBusy(true);
    setErr(null);
    try {
      await api.unsilenceIncident(incident.id);
      onChange?.();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const selectCls =
    "rounded-lg border border-slate-300 px-2 py-1 text-xs outline-none focus:border-teal focus:ring-2 focus:ring-teal/20";

  return (
    <div className="mt-3 space-y-3 rounded-lg border border-slate-200 bg-slate-50 p-3">
      {silenced ? (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="rounded-full bg-slate-200 px-2 py-0.5 font-semibold text-slate-600">Silenced</span>
          <span className="text-slate-500">
            {incident.silence_scope === "monitor" ? "whole monitor" : "this incident"}
            {incident.silenced_until
              ? ` · until ${fmtDateTime(incident.silenced_until)}`
              : ""}
          </span>
          {incident.ticket_url && (
            <a href={incident.ticket_url} className="text-teal-700 hover:underline" target="_blank" rel="noreferrer">
              ticket
            </a>
          )}
          <Button variant="ghost" className="!px-2 !py-1 text-xs" onClick={lift} disabled={busy}>
            Unsilence
          </Button>
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-medium text-slate-500">Silence</span>
            <select className={selectCls} value={scope} onChange={(e) => setScope(e.target.value)}>
              <option value="incident">this incident</option>
              <option value="monitor">the whole monitor</option>
            </select>
            <span className="text-xs text-slate-500">for</span>
            <select
              className={selectCls}
              value={String(choice)}
              onChange={(e) => setChoice(e.target.value === "custom" ? "custom" : Number(e.target.value))}
            >
              {DURATIONS.map((d) => (
                <option key={d.label} value={String(d.hours)}>
                  {d.label}
                </option>
              ))}
            </select>
            {choice === "custom" && (
              <input
                type="datetime-local"
                className={selectCls}
                value={until}
                onChange={(e) => setUntil(e.target.value)}
              />
            )}
          </div>
          <TextInput value={note} onChange={(e) => setNote(e.target.value)} placeholder="Reason (kept as a note)…" className="!py-1 text-xs" />
          <TextInput value={ticket} onChange={(e) => setTicket(e.target.value)} placeholder="Ticket URL (optional)…" className="!py-1 text-xs" />
          <Button variant="ghost" className="!px-3 !py-1 text-xs" onClick={silence} disabled={busy}>
            Silence alert
          </Button>
        </>
      )}
      {err && <p className="text-xs text-rose-600">{err}</p>}
    </div>
  );
}

const KIND_LABEL: Record<string, string> = {
  anomaly: "Anomaly",
  rule: "Rule",
  schema_drift: "Schema drift",
  quality: "Quality",
  compare: "Compare",
  dbt_test: "dbt test",
  dbt_run: "dbt model",
};

function guessKey(evidence: Record<string, unknown>): string {
  const rows = evidence.failing_keys;
  if (!Array.isArray(rows) || rows.length === 0 || typeof rows[0] !== "object" || rows[0] === null) return "";
  return Object.keys(rows[0] as Record<string, unknown>)
    .filter((name) => /(^id$|_id)$/i.test(name))
    .join(", ");
}

export function failedCompareJobId(i: Incident): string | null {
  const cmp = i.evidence?.compare;
  if (!cmp || typeof cmp !== "object" || (cmp as { status?: unknown }).status !== "failed") return null;
  return compareJobId(i);
}

/** A compare that never ran is still on the record. It is not an open data incident. */
export function isUnrunCompare(i: Incident): boolean {
  return i.status !== "resolved" && failedCompareJobId(i) != null;
}

export function CompareRunError({ jobId }: { jobId: string }) {
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api.getCompare(jobId).then((r) => {
      if (live) setError(r.data.error);
    }).catch(() => undefined);
    return () => {
      live = false;
    };
  }, [jobId]);
  if (!error) return null;
  return <div className="mt-0.5 text-sm text-slate-600">{error}</div>;
}

function compareJobId(i: Incident): string | null {
  const cmp = i.evidence?.compare;
  if (cmp && typeof cmp === "object" && typeof (cmp as { job_id?: unknown }).job_id === "string") {
    return (cmp as { job_id: string }).job_id;
  }
  return typeof i.evidence?.job_id === "string" ? i.evidence.job_id : null;
}

function splitRef(tableRef: string | null): { database: string; schema: string; table: string } {
  const parts = (tableRef || "").split(".").filter(Boolean);
  const table = parts.pop() ?? "";
  const schema = parts.pop() ?? "";
  const database = parts.pop() ?? "";
  return { database, schema, table };
}

function Evidence({ e }: { e: Record<string, unknown> }) {
  const num = (v: unknown) => (typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(v));
  const rows: [string, string][] = [];
  if (e.metric) rows.push(["metric", String(e.metric)]);
  if (e.value !== undefined && e.value !== null) rows.push(["value", num(e.value)]);
  if (e.threshold !== undefined && e.threshold !== null) rows.push(["threshold", num(e.threshold)]);
  if (e.lo !== undefined && e.lo !== null && e.hi !== undefined && e.hi !== null)
    rows.push(["expected band", `${num(e.lo)} – ${num(e.hi)}`]);
  if (e.score !== undefined && e.score !== null) rows.push(["deviation", `${num(e.score)}σ`]);
  if (e.direction) rows.push(["direction", String(e.direction)]);
  if (e.unique_id) rows.push(["dbt", String(e.unique_id)]);
  if (e.failures !== undefined && e.failures !== null) rows.push(["failures", num(e.failures)]);
  if (e.message) rows.push(["message", String(e.message)]);
  const cmp = e.compare;
  if (cmp && typeof cmp === "object") {
    const c = cmp as { status?: unknown; match?: unknown };
    const label = c.status === "failed" ? "failed" : c.match === true ? "match" : c.match === false ? "differences" : String(c.status ?? "");
    if (label) rows.push(["compare", label]);
  }
  const keys = Array.isArray(e.failing_keys) ? (e.failing_keys as Record<string, unknown>[]) : [];
  const sampleSql = typeof e.sample_sql === "string" ? e.sample_sql : null;
  if (rows.length === 0 && keys.length === 0) return null;
  return (
    <div>
      {rows.length > 0 && (
        <table className="mt-2 text-xs">
          <tbody>
            {rows.map(([k, v]) => (
              <tr key={k}>
                <td className="pr-3 py-0.5 font-medium text-slate-400">{k}</td>
                <td className="py-0.5 font-mono text-slate-600">{v}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {keys.length > 0 && <FailingKeys keys={keys} sql={sampleSql} />}
      {Array.isArray(e.also_used_by) && e.also_used_by.length > 0 && (
        <p className="mt-2 text-xs text-slate-500">Also used by {e.also_used_by.join(", ")}.</p>
      )}
    </div>
  );
}

export function IncidentList({
  incidents,
  onAck,
  onResolve,
  onChange,
  showTable = true,
  empty = "No incidents.",
}: {
  incidents: Incident[];
  onAck: (id: string) => void;
  onResolve: (id: string) => void;
  onChange?: () => void; // called after a triage action (resolve/reopen) so the list can refresh
  showTable?: boolean;
  empty?: string;
}) {
  const nav = useNavigate();
  const [openId, setOpenId] = useState<string | null>(null);
  const [triageId, setTriageId] = useState<string | null>(null);
  const [silenceId, setSilenceId] = useState<string | null>(null);
  const startCompare = async (i: Incident) => {
    const mon = i.monitor_id ? await api.getMonitor(i.monitor_id).catch(() => null) : null;
    const ref = splitRef(i.table_ref);
    const q = new URLSearchParams({
      incident: i.id,
      connection: mon?.data.connection_id ?? "",
      database: ref.database,
      schema: ref.schema,
      table: ref.table,
      key: guessKey(i.evidence),
    });
    nav(`/validation/from-incident?${q}`);
  };
  if (incidents.length === 0) return <div className="text-sm text-slate-500">{empty}</div>;
  return (
    <div className="space-y-2">
      {incidents.map((i) => {
        const resolved = i.status === "resolved";
        const jobId = compareJobId(i);
        return (
          <div
            key={i.id}
            className={`rounded-lg border p-3 ${resolved ? "border-slate-100 opacity-60" : "border-slate-200"}`}
          >
            <div className="flex items-start gap-3">
              <HealthBadge status={sevToHealth(String(i.severity))} label={String(i.severity)} />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-slate-500">
                    {KIND_LABEL[i.kind ?? ""] ?? i.kind ?? "incident"}
                  </span>
                  {i.dimension && <span className="text-[10px] uppercase text-slate-400">{i.dimension}</span>}
                  {i.occurrences > 1 && (
                    <span className="rounded-full bg-amber-100 px-1.5 text-[10px] font-semibold text-amber-700">
                      ×{i.occurrences}
                    </span>
                  )}
                  <span
                    className={`text-[10px] uppercase ${resolved ? "text-emerald-600" : i.status === "acknowledged" ? "text-sky-600" : "text-rose-600"}`}
                  >
                    {i.status}
                  </span>
                  {i.silenced && (
                    <span className="rounded-full bg-slate-200 px-1.5 text-[10px] font-semibold text-slate-600">
                      silenced
                    </span>
                  )}
                </div>
                <div className="mt-0.5 text-sm text-navy">{i.summary}</div>
                {failedCompareJobId(i) && <CompareRunError jobId={failedCompareJobId(i)!} />}
                {showTable && i.table_ref && (
                  <div className="text-xs text-slate-400">{i.table_ref}</div>
                )}
                {jobId && (
                  <Link to={`/validation/${jobId}`} className="text-xs text-teal-700 hover:underline">
                    Open compare
                  </Link>
                )}
                {openId === i.id && <Evidence e={i.evidence} />}
                {triageId === i.id && <Triage incident={i} onChange={onChange} />}
                {silenceId === i.id && <SilenceControl incident={i} onChange={onChange} />}
              </div>
              <div className="flex shrink-0 flex-col items-end gap-1">
                <button
                  onClick={() => setOpenId(openId === i.id ? null : i.id)}
                  className="text-xs text-slate-400 hover:text-slate-600"
                >
                  {openId === i.id ? "hide" : "evidence"}
                </button>
                <button
                  onClick={() => setTriageId(triageId === i.id ? null : i.id)}
                  className="text-xs text-teal-700 hover:text-teal-800"
                >
                  {triageId === i.id ? "close" : "notes & triage"}
                </button>
                {!resolved && (
                  <button
                    onClick={() => setSilenceId(silenceId === i.id ? null : i.id)}
                    className="text-xs text-slate-500 hover:text-slate-700"
                  >
                    {silenceId === i.id ? "close" : i.silenced ? "silenced" : "silence"}
                  </button>
                )}
                {!resolved && (
                  <div className="flex gap-1">
                    {i.status === "open" && (
                      <Button variant="ghost" className="!px-2 !py-1 text-xs" onClick={() => onAck(i.id)}>
                        Ack
                      </Button>
                    )}
                    {i.table_ref && !jobId && (
                      <Button variant="ghost" className="!px-2 !py-1 text-xs" onClick={() => startCompare(i)}>
                        Compare to source
                      </Button>
                    )}
                    <Button variant="ghost" className="!px-2 !py-1 text-xs" onClick={() => onResolve(i.id)}>
                      Resolve
                    </Button>
                  </div>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
