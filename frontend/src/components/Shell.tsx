import type { ReactNode } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";
import { usePrefs } from "../prefs";
import { tzLabel } from "../tz";
import { Icon } from "./vex";
import { QueueBell } from "./QueueBell";
import { ProjectSwitcher } from "./ProjectSwitcher";

const NAV = [
  { to: "/validation", label: "Data Validation", icon: "validation" as const, match: "/validation" },
  { to: "/find-column", label: "Find Column", icon: "search" as const, match: "/find-column" },
  { to: "/observability", label: "Data Observability", icon: "observability" as const, match: "/observability" },
  { to: "/admin", label: "Administration", icon: "admin" as const, match: "/admin", admin: true },
];

const ROLE_LABEL: Record<string, string> = {
  org_admin: "Organization admin",
  group_admin: "Group admin",
  editor: "Editor",
  viewer: "Viewer",
  report_viewer: "Report viewer",
};

function Sidebar() {
  const { can, role } = usePrefs();
  const loc = useLocation();
  const isAdmin = can("manage_group") || can("manage_connections") || can("manage_org");
  const initials = (ROLE_LABEL[role] ?? role ?? "U")
    .split(" ")
    .map((w) => w[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();

  return (
    <aside className="fixed inset-y-0 left-0 z-20 flex w-60 flex-col border-r border-slate-200 bg-white">
      <div className="flex h-14 items-center gap-2 px-5">
        <span className="grid h-6 w-6 place-items-center rounded-md bg-teal text-sm font-bold text-white">◆</span>
        <span className="text-[15px] font-extrabold tracking-tight text-navy">Data Validator</span>
      </div>

      <div className="px-3 pt-4">
        <div className="px-3 pb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-400">Workspace</div>
        <nav className="space-y-0.5">
          {NAV.filter((n) => !n.admin || isAdmin).map((n) => {
            const active = loc.pathname.startsWith(n.match);
            return (
              <NavLink
                key={n.to}
                to={n.to}
                className={`flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition ${
                  active ? "bg-teal/10 text-teal-700" : "text-slate-600 hover:bg-slate-50"
                }`}
              >
                <Icon name={n.icon} className={`h-[18px] w-[18px] ${active ? "text-teal-600" : "text-slate-400"}`} />
                {n.label}
              </NavLink>
            );
          })}
        </nav>
      </div>

      <div className="mt-auto space-y-3 p-3">
        <a className="flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm text-slate-500 hover:bg-slate-50" href="/docs" target="_blank" rel="noreferrer">
          <Icon name="help" className="h-[18px] w-[18px] text-slate-400" />
          Help &amp; documentation
        </a>
        <Link to="/settings" className="flex items-center gap-2.5 rounded-lg border border-slate-200 px-3 py-2 hover:bg-slate-50">
          <span className="grid h-8 w-8 place-items-center rounded-full bg-navy text-xs font-semibold text-white">
            {initials || "U"}
          </span>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-navy">Signed in</div>
            <div className="truncate text-xs text-slate-400">{ROLE_LABEL[role] ?? role ?? "—"} · Preferences</div>
          </div>
        </Link>
      </div>
    </aside>
  );
}

function TopBar() {
  const { timezone } = usePrefs();
  const nav = useNavigate();
  return (
    <header className="sticky top-0 z-10 flex h-14 items-center justify-between border-b border-slate-200 bg-white/90 px-6 backdrop-blur">
      <div className="flex items-center gap-2 text-sm">
        <span className="text-slate-400">Project</span>
        <ProjectSwitcher variant="bar" />
        <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-inset ring-emerald-200">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
          Production
        </span>
      </div>
      <div className="flex items-center gap-4 text-slate-400">
        <Link to="/settings" title={`Timezone: ${timezone} — change in Preferences`} className="text-xs font-medium hover:text-slate-600">
          {tzLabel(timezone)}
        </Link>
        <QueueBell />
        <button onClick={() => nav("/settings")} className="hover:text-slate-600"><Icon name="gear" className="h-[18px] w-[18px]" /></button>
      </div>
    </header>
  );
}

export default function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-full bg-page">
      <Sidebar />
      <div className="pl-60">
        <TopBar />
        <main className="mx-auto max-w-[1180px] px-8 py-7">{children}</main>
      </div>
    </div>
  );
}
