import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../api";
import type { Feedback } from "../../types";
import { Button, Card, EmptyState, Spinner } from "../../components/ui";

const CAT_LABEL: Record<string, string> = {
  idea: "💡 Idea", issue: "🐞 Issue", praise: "🎉 Praise", other: "💬 Other",
};
const STATUS_TONE: Record<string, string> = {
  new: "bg-teal/10 text-teal-700",
  reviewed: "bg-slate-100 text-slate-600",
  closed: "bg-slate-100 text-slate-400",
};
const FILTERS = ["all", "new", "reviewed", "closed"] as const;

function when(ts: string | null) {
  if (!ts) return "—";
  const d = new Date(ts);
  return d.toLocaleString();
}

export default function AdminFeedback() {
  const [list, setList] = useState<Feedback[] | null>(null);
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("all");
  const [err, setErr] = useState<string | null>(null);

  const reload = useCallback(() => {
    setList(null);
    api.listFeedback().then((r) => setList(r.data)).catch((e) => setErr((e as Error).message));
  }, []);
  useEffect(reload, [reload]);

  const shown = useMemo(
    () => (list ?? []).filter((f) => filter === "all" || f.status === filter),
    [list, filter],
  );
  const counts = useMemo(() => {
    const c: Record<string, number> = { all: list?.length ?? 0, new: 0, reviewed: 0, closed: 0 };
    for (const f of list ?? []) c[f.status] = (c[f.status] ?? 0) + 1;
    return c;
  }, [list]);

  const setStatus = async (f: Feedback, status: string) => {
    try {
      await api.updateFeedbackStatus(f.id, status);
      setList((prev) => (prev ?? []).map((x) => (x.id === f.id ? { ...x, status: status as Feedback["status"] } : x)));
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-lg font-bold text-navy">Feedback</h1>
        <p className="mt-1 max-w-2xl text-sm text-slate-500">
          In-app feedback submitted by users from the floating “Feedback” button on any page. Each item
          captures the page, URL, and browser context so you can act on it without a repro.
        </p>
      </div>

      {err && <div className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700">{err}</div>}

      <div className="flex flex-wrap items-center gap-2">
        {FILTERS.map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`rounded-full px-3 py-1 text-xs font-medium capitalize ${
              filter === f ? "bg-navy text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"
            }`}
          >
            {f} <span className="opacity-70">{counts[f] ?? 0}</span>
          </button>
        ))}
        <Button variant="ghost" onClick={reload} className="ml-auto">Refresh</Button>
      </div>

      {list === null ? (
        <Spinner label="Loading…" />
      ) : shown.length === 0 ? (
        <EmptyState title="No feedback yet" hint="Feedback submitted from any page shows up here." />
      ) : (
        <div className="space-y-3">
          {shown.map((f) => (
            <Card key={f.id} className="p-4">
              <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
                <span className="font-medium text-slate-700">{CAT_LABEL[f.category] ?? f.category}</span>
                {f.rating != null && <span title="rating">{"⭐".repeat(f.rating)}</span>}
                <span className={`rounded px-1.5 py-0.5 font-semibold uppercase tracking-wide ${STATUS_TONE[f.status] ?? ""}`}>
                  {f.status}
                </span>
                <span className="ml-auto">{when(f.created_at)}</span>
              </div>

              <p className="mt-2 whitespace-pre-wrap text-sm text-navy">{f.message}</p>

              <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-slate-400">
                <span>Page: <span className="font-mono text-slate-500">{f.page ?? "—"}</span></span>
                {f.context?.viewport ? <span>Viewport: {String(f.context.viewport)}</span> : null}
                {f.user_id ? <span>User: <span className="font-mono">{f.user_id.slice(0, 8)}…</span></span> : null}
                {f.url ? (
                  <a href={f.url} target="_blank" rel="noreferrer" className="truncate text-slate-500 underline hover:text-teal-700" title={f.url}>
                    open page
                  </a>
                ) : null}
              </div>
              {f.user_agent && (
                <div className="mt-1 truncate text-[11px] text-slate-300" title={f.user_agent}>{f.user_agent}</div>
              )}

              <div className="mt-3 flex gap-2">
                {f.status !== "reviewed" && <Button variant="ghost" onClick={() => setStatus(f, "reviewed")}>Mark reviewed</Button>}
                {f.status !== "closed" && <Button variant="ghost" onClick={() => setStatus(f, "closed")}>Close</Button>}
                {f.status !== "new" && <Button variant="ghost" onClick={() => setStatus(f, "new")}>Reopen</Button>}
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
