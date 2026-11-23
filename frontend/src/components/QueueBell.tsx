import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import type { ActiveJob, PoolStats } from "../types";
import { Icon } from "./vex";

const KIND: Record<string, string> = { count: "Counts", schema: "Schema", data: "Data validation" };

/** Top-bar queue bell: shows how many compares are running now and how many are waiting for a
 *  worker slot (we run a bounded number in parallel). Click to see the live list. Polls while
 *  mounted — a little faster when there's work in flight. */
export function QueueBell() {
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<{ jobs: ActiveJob[]; running: number; queued: number; total: number; concurrency: number | null; pool?: PoolStats } | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    const tick = () => api.activeJobs().then((r) => { if (alive) setData(r.data); }).catch(() => undefined);
    tick();
    // Poll faster while something is active so the queue feels live; idle otherwise.
    const id = setInterval(tick, (data?.total ?? 0) > 0 ? 2500 : 6000);
    return () => { alive = false; clearInterval(id); };
  }, [data?.total]);

  // Close on outside click.
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => { if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  const total = data?.total ?? 0;
  const running = data?.running ?? 0;
  const queued = data?.queued ?? 0;

  const go = (j: ActiveJob) => {
    setOpen(false);
    nav(j.batch_id ? `/validation/batch/${j.batch_id}` : `/validation/${j.id}`);
  };

  return (
    <div className="relative" ref={boxRef}>
      <button onClick={() => setOpen((o) => !o)} title={total ? `${running} running · ${queued} waiting` : "No running jobs"}
        className="relative hover:text-slate-600">
        <Icon name="bell" className="h-[18px] w-[18px]" />
        {total > 0 && (
          <span className={`absolute -right-2 -top-2 flex min-w-[16px] items-center justify-center rounded-full px-1 text-[10px] font-bold text-white ${running > 0 ? "bg-sky-500" : "bg-amber-500"}`}>
            {total}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 top-8 z-50 w-80 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl">
          <div className="border-b border-slate-100 px-4 py-2.5">
            <div className="flex items-center justify-between">
              <span className="text-sm font-semibold text-navy">Job queue</span>
              <span className="text-[11px] text-slate-400">{running} running · {queued} waiting</span>
            </div>
            {data?.pool && (
              <div className="mt-1 text-[11px] text-slate-400">
                pool: {data.pool.used_memory_gb}/{data.pool.total_memory_gb} GB · {data.pool.used_vcpus}/{data.pool.total_vcpus} vCPU
                {data.pool.waiting_for_resources > 0 && <span className="text-amber-600"> · {data.pool.waiting_for_resources} waiting on resources</span>}
              </div>
            )}
          </div>
          {total === 0 ? (
            <p className="px-4 py-6 text-center text-xs text-slate-400">Nothing running or waiting.</p>
          ) : (
            <div className="max-h-96 overflow-auto">
              {data!.jobs.map((j) => (
                <button key={j.id} onClick={() => go(j)}
                  className="flex w-full items-center gap-2 border-b border-slate-50 px-4 py-2 text-left last:border-0 hover:bg-slate-50/70">
                  {j.status === "running" ? (
                    <span className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-sky-500" title="running" />
                  ) : (
                    <span className="h-2 w-2 shrink-0 rounded-full bg-amber-400" title="waiting" />
                  )}
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium text-navy">{j.batch_name || j.name}</span>
                    <span className="block truncate text-[11px] text-slate-400">
                      {KIND[j.kind] ?? j.kind}{j.batch_id ? " · batch" : ""}{j.last_run_by_name ? ` · ${j.last_run_by_name}` : ""}
                    </span>
                  </span>
                  <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${j.status === "running" ? "bg-sky-50 text-sky-700" : "bg-amber-50 text-amber-700"}`}>
                    {j.status === "running" ? "running" : "waiting"}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
