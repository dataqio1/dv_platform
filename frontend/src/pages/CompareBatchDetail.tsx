import { Fragment, useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import type { CompareBatch } from "../types";
import { Button, Card, HealthBadge, Spinner } from "../components/ui";
import { SortTh, useSort } from "../components/sortable";
import { ExecutionLog } from "../components/ExecutionLog";
import { ScheduleControl } from "../components/ScheduleControl";

function jobBadge(status: string, summary?: Record<string, unknown> | null) {
  if (status === "queued" || status === "running") return <HealthBadge status="warning" label="Running" />;
  if (status === "cancelled") return <HealthBadge status="warning" label="Cancelled" />;
  if (status === "failed") return <HealthBadge status="unknown" label="Did not run" />;
  const m = (summary as { match?: boolean } | null | undefined)?.match;
  if (m === true) return <HealthBadge status="healthy" label="Match" />;
  if (m === false) return <HealthBadge status="critical" label="Differences" />;
  return <HealthBadge status="unknown" label={status} />;
}

export default function CompareBatchDetail() {
  const { batchId = "" } = useParams();
  const navigate = useNavigate();
  const [batch, setBatch] = useState<CompareBatch | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [logOpen, setLogOpen] = useState<string | null>(null);  // job id whose log is expanded

  const load = useCallback(() => {
    api.getBatch(batchId).then((r) => setBatch(r.data)).catch((e) => setErr(e.message));
  }, [batchId]);
  useEffect(() => { load(); }, [load]);

  // Set/clear the batch schedule. A batch is scheduled via any of its pair jobs (the backend
  // applies it to the whole batch), matching the old list-row behaviour.
  const onSetSchedule = async (cron: string | null) => {
    const jid = batch?.jobs?.[0]?.id;
    if (!jid) return;
    try {
      await api.setSchedule(jid, cron);
      load();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const finished = (batch?.jobs ?? []).every((j) => j.status === "succeeded" || j.status === "failed");

  const signOff = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.signOffBatch(batchId);
      navigate(`/reports/${r.data.id}`);
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };

  const onDelete = async () => {
    if (!confirm("Delete this batch? All test cases, results and stored samples will be removed.")) return;
    setBusy(true);
    try {
      await api.deleteBatch(batchId);
      navigate("/validation");
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };

  const jobSort = useSort(batch?.jobs ?? [], {
    table: (j) => j.source?.table || j.name || j.id,
    result: (j) => j.status,
  });

  if (err) return <p className="text-sm text-rose-600">{err}</p>;
  if (!batch) return <Spinner />;

  return (
    <div className="space-y-6">
      <div>
        <Link
          to={batch.project_id ? `/validation/projects/${batch.project_id}?tab=runs` : "/validation"}
          className="text-sm text-slate-400 hover:text-slate-600"
        >← {batch.project_id ? "Back to executions" : "Data Validation"}</Link>
        <div className="mt-1 flex items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold text-navy">{batch.name}</h1>
            <span className="rounded bg-slate-100 px-2 py-0.5 text-xs">{batch.kind}</span>
            {batch.status === "succeeded" ? (
              <HealthBadge status="healthy" label="All match" />
            ) : batch.status === "differences" ? (
              <HealthBadge status="critical" label="Differences" />
            ) : batch.status === "running" ? (
              <HealthBadge status="warning" label="Running" />
            ) : batch.status === "cancelled" ? (
              <HealthBadge status="warning" label="Cancelled" />
            ) : batch.status === "partial" ? (
              <HealthBadge status="warning" label="Partial" />
            ) : (
              <HealthBadge status="critical" label="Failed" />
            )}
          </div>
          <div className="flex items-center gap-2">
            <Link to={`/validation/batch/${batch.batch_id}/edit`}>
              <Button variant="ghost">Edit</Button>
            </Link>
            {finished && (
              <Button onClick={signOff} disabled={busy}>
                {busy ? "Signing off…" : "Sign-off"}
              </Button>
            )}
            <a href={api.batchExportUrl(batchId, "pdf")}>
              <Button variant="ghost">Export PDF</Button>
            </a>
            <a href={api.batchExportUrl(batchId, "csv")}>
              <Button variant="ghost">CSV</Button>
            </a>
            <Button variant="ghost" onClick={onDelete} disabled={busy}>
              {busy ? "Deleting…" : "Delete"}
            </Button>
          </div>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-400">
          <span>{batch.total} tables · {batch.succeeded} ok · {batch.failed} failed · {batch.matched} match</span>
          <span className="flex items-center gap-1">
            · Schedule:
            <ScheduleControl cron={batch.schedule_cron ?? null} nextRun={batch.next_run ?? null} onSet={onSetSchedule} />
          </span>
        </div>
      </div>

      <Card>
        <table className="w-full text-sm">
          <thead className="border-b border-slate-200 bg-slate-50/60 text-left text-[11px] uppercase tracking-wide text-slate-400">
            <tr>
              <SortTh label="Table" columnKey="table" sort={jobSort.sort} onSort={jobSort.requestSort} className="px-5 py-3 font-medium" />
              <SortTh label="Result" columnKey="result" sort={jobSort.sort} onSort={jobSort.requestSort} className="px-5 py-3 font-medium" />
              <SortTh label="Summary" sortable={false} sort={jobSort.sort} onSort={jobSort.requestSort} className="px-5 py-3 font-medium" />
              <th className="px-5 py-3" />
            </tr>
          </thead>
          <tbody>
            {jobSort.sorted.map((j) => {
              const s = (j.summary ?? {}) as Record<string, number>;
              const text =
                j.kind === "count"
                  ? `src ${s.src_count} vs dst ${s.dst_count}`
                  : j.kind === "schema"
                  ? `${s.matched} matched · ${s.type_mismatches} type · ${s.source_only}/${s.target_only} only`
                  : `${s.match_rate_pct ?? "?"}% · ${s.value_difference_rows ?? s.mismatched_rows ?? 0} diff · ${s.source_only_rows ?? 0}/${s.target_only_rows ?? 0} only`;
              return (
                <Fragment key={j.id}>
                  <tr className="border-b border-slate-50 hover:bg-slate-50/60 last:border-0">
                    <td className="px-5 py-3">
                      <Link to={`/validation/${j.id}`} className="font-medium text-teal-700 hover:underline">
                        {(j.source.table ?? j.name) as string}
                      </Link>
                    </td>
                    <td className="px-5 py-3">{jobBadge(j.status, j.summary)}</td>
                    <td className="px-5 py-3 text-xs text-slate-500">{j.error ? <span className="text-rose-600">{j.error}</span> : text}</td>
                    <td className="px-5 py-3 text-right">
                      <button onClick={() => setLogOpen((cur) => (cur === j.id ? null : j.id))}
                        className="text-xs font-medium text-slate-500 hover:text-teal-700">
                        {logOpen === j.id ? "Hide logs" : "Logs"}
                      </button>
                    </td>
                  </tr>
                  {logOpen === j.id && (
                    <tr className="bg-slate-50/40">
                      <td colSpan={4} className="px-5 py-3">
                        <ExecutionLog jobId={j.id} defaultOpen />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
