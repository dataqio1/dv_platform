import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useProjects } from "../activeProject";
import type { Project } from "../types";
import { Icon } from "./vex";

/** Searchable project switcher. Shows each project's name, description, test-case count and tags
 *  so you can tell them apart — far clearer than a bare <select>, and ready for richer metadata.
 *  `variant="chip"` is the bordered breadcrumb button; `"bar"` is the lighter top-bar trigger. */
export function ProjectSwitcher({ variant = "chip" }: { variant?: "chip" | "bar" }) {
  const { projects, active } = useProjects();
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const boxRef = useRef<HTMLDivElement>(null);

  const current = projects.find((p) => p.id === active) ?? null;

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => { if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onClick); document.removeEventListener("keydown", onKey); };
  }, [open]);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return projects;
    return projects.filter((p) =>
      `${p.name} ${p.description ?? ""} ${(p.tags || []).join(" ")}`.toLowerCase().includes(needle));
  }, [projects, q]);

  const pick = (p: Project) => { setOpen(false); nav(`/validation/projects/${p.id}`); };

  const trigger =
    variant === "bar"
      ? "inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-2.5 py-1 text-sm font-semibold text-navy hover:border-teal"
      : "inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-sm font-semibold text-navy hover:border-teal";

  return (
    <div className="relative" ref={boxRef}>
      <button onClick={() => { setOpen((o) => !o); setQ(""); }} className={trigger}>
        <span className="max-w-[16rem] truncate">{current?.name ?? "Select project"}</span>
        <Icon name="chevron" className="h-3.5 w-3.5 text-slate-400" />
      </button>

      {open && (
        <div className="absolute left-0 top-full z-50 mt-1.5 w-96 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl">
          <div className="border-b border-slate-100 p-2">
            <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search projects by name, description, tag…"
              className="w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-teal" />
          </div>
          <div className="max-h-80 overflow-auto">
            {projects.length === 0 ? (
              <p className="px-4 py-6 text-center text-xs text-slate-400">No projects yet.</p>
            ) : filtered.length === 0 ? (
              <p className="px-4 py-6 text-center text-xs text-slate-400">No projects match “{q}”.</p>
            ) : (
              filtered.map((p) => {
                const isCurrent = p.id === current?.id;
                const count = (p.compare_count ?? 0) + (p.monitor_count ?? 0);
                return (
                  <button key={p.id} onClick={() => pick(p)}
                    className={`flex w-full items-start gap-2.5 border-b border-slate-50 px-4 py-2.5 text-left last:border-0 hover:bg-slate-50/70 ${isCurrent ? "bg-teal/5" : ""}`}>
                    <span className={`mt-1 h-2 w-2 shrink-0 rounded-full ${isCurrent ? "bg-teal" : "bg-slate-200"}`} />
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-2">
                        <span className="truncate text-sm font-semibold text-navy">{p.name}</span>
                        {isCurrent && <span className="shrink-0 rounded-full bg-teal/10 px-1.5 py-0.5 text-[10px] font-medium text-teal-700">current</span>}
                      </span>
                      {p.description && <span className="mt-0.5 block truncate text-xs text-slate-500">{p.description}</span>}
                      <span className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-slate-400">
                        <span>{count} test case{count === 1 ? "" : "s"}</span>
                        {(p.tags || []).map((t) => (
                          <span key={t} className="rounded-full bg-slate-100 px-1.5 py-0.5 text-slate-500">{t}</span>
                        ))}
                      </span>
                    </span>
                  </button>
                );
              })
            )}
          </div>
          <Link to="/validation" onClick={() => setOpen(false)}
            className="block border-t border-slate-100 px-4 py-2 text-center text-xs font-medium text-teal-700 hover:bg-slate-50">
            View all projects →
          </Link>
        </div>
      )}
    </div>
  );
}
