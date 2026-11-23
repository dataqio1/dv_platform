import { useEffect, useState } from "react";
import { NavLink, Navigate, Outlet } from "react-router-dom";
import { usePrefs } from "../../prefs";
import { api } from "../../api";
import type { SystemInfo } from "../../types";
import { Spinner } from "../../components/ui";

// Each admin section + the permission that unlocks it. The rail shows only what the
// current role can reach; the tab itself is hidden entirely for non-admins.
const SECTIONS = [
  { to: "members", label: "Members & Roles", perm: "manage_group" },
  { to: "access", label: "People & Access", perm: "manage_group" },
  { to: "connections", label: "Connections", perm: "manage_connections" },
  { to: "etl-tools", label: "ETL Tools", perm: "manage_group" },
  { to: "compute", label: "Compute", perm: "manage_group" },
  { to: "integrations", label: "Integrations", perm: "manage_group" },
  { to: "organization", label: "Organization", perm: "manage_group" },
  { to: "feedback", label: "Feedback", perm: "manage_group" },
  { to: "audit", label: "Audit Log", perm: "manage_group" },
  { to: "system", label: "System", perm: "manage_group" },
];

export default function AdminLayout() {
  const { can, meLoaded } = usePrefs();
  const [sys, setSys] = useState<SystemInfo | null>(null);

  useEffect(() => {
    api.systemInfo().then((r) => setSys(r.data)).catch(() => undefined);
  }, []);

  // Wait for /me before deciding — permissions arrive async, don't bounce too early.
  if (!meLoaded) return <Spinner />;
  const visible = SECTIONS.filter((s) => can(s.perm));
  if (visible.length === 0) return <Navigate to="/observability" replace />;

  const link = (active: boolean) =>
    `block rounded-md px-3 py-2 text-sm font-medium transition ${
      active ? "bg-teal/10 text-teal-700" : "text-slate-600 hover:bg-slate-50"
    }`;

  return (
    <div className="flex flex-col gap-8 md:flex-row">
      <aside className="w-full shrink-0 md:w-56">
        <h1 className="mb-3 text-lg font-bold text-navy">Admin</h1>
        <nav className="space-y-1">
          {visible.map((s) => (
            <NavLink key={s.to} to={s.to} className={({ isActive }) => link(isActive)}>
              {s.label}
            </NavLink>
          ))}
        </nav>
        {sys && (
          <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-500">
            <div>
              mode <span className="font-semibold text-navy">{sys.profile}</span>
            </div>
            <div className="mt-0.5">identity · {sys.identity_provider}</div>
            <div className="mt-0.5">v{sys.version}</div>
          </div>
        )}
      </aside>
      <div className="min-w-0 flex-1">
        <Outlet />
      </div>
    </div>
  );
}
