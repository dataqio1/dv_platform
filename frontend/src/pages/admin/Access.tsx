import { useEffect, useState } from "react";
import { api } from "../../api";
import type { RoleMatrix, ServiceAccount } from "../../types";
import { Button, Card, EmptyState, Field, Spinner, TextInput } from "../../components/ui";

const PERM_LABEL: Record<string, string> = {
  read: "Read",
  write: "Write",
  view_reports: "View reports",
  manage_connections: "Connections",
  manage_projects: "Projects",
  manage_group: "Group admin",
  manage_org: "Org admin",
};

const ROLE_LABEL: Record<string, string> = {
  report_viewer: "Report viewer",
  viewer: "Viewer",
  editor: "Editor",
  group_admin: "Group admin",
  org_admin: "Org admin",
};

// Assignable to a service account (the backend rejects org_admin).
const SA_ROLES = ["viewer", "editor", "group_admin"];

function RoleMatrixTable({ matrix }: { matrix: RoleMatrix }) {
  return (
    <Card className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <th className="px-4 py-2 text-left font-semibold">Role</th>
            {matrix.permissions.map((p) => (
              <th key={p} className="px-3 py-2 text-center font-semibold">
                {PERM_LABEL[p] ?? p}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {matrix.roles.map((r) => (
            <tr key={r.role} className="border-b border-slate-100 last:border-0">
              <td className="whitespace-nowrap px-4 py-2 font-medium text-navy">
                {ROLE_LABEL[r.role] ?? r.role}
              </td>
              {matrix.permissions.map((p) => (
                <td key={p} className="px-3 py-2 text-center">
                  {r.matrix[p] ? (
                    <span className="text-emerald-600">✓</span>
                  ) : (
                    <span className="text-slate-300">·</span>
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function ServiceAccounts() {
  const [rows, setRows] = useState<ServiceAccount[] | null>(null);
  const [name, setName] = useState("");
  const [role, setRole] = useState("viewer");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [newKey, setNewKey] = useState<{ name: string; api_key: string } | null>(null);

  const load = () =>
    api.listServiceAccounts().then((r) => setRows(r.data)).catch((e) => setErr((e as Error).message));
  useEffect(() => {
    load();
  }, []);

  const create = async () => {
    if (!name.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await api.createServiceAccount({ name: name.trim(), role });
      setNewKey({ name: r.data.name, api_key: r.data.api_key ?? "" });
      setName("");
      load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const rotate = async (sa: ServiceAccount) => {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.rotateServiceAccountKey(sa.id);
      setNewKey({ name: r.data.name, api_key: r.data.api_key ?? "" });
      load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const remove = async (sa: ServiceAccount) => {
    if (!confirm(`Delete service account "${sa.name}"? Its API key stops working immediately.`)) return;
    setBusy(true);
    try {
      await api.deleteServiceAccount(sa.id);
      load();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-navy">Service identities</h3>
        <p className="text-xs text-slate-500">
          Non-human accounts with an API key. The key is shown only once — copy it now.
        </p>
      </div>

      {newKey && (
        <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-3">
          <div className="text-xs font-semibold text-emerald-800">
            API key for “{newKey.name}” — copy it now, it won’t be shown again
          </div>
          <code className="mt-1 block break-all rounded bg-white px-2 py-1 font-mono text-xs text-slate-800">
            {newKey.api_key}
          </code>
          <button className="mt-1 text-xs text-emerald-700 hover:underline" onClick={() => setNewKey(null)}>
            Dismiss
          </button>
        </div>
      )}

      <Card className="p-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex-1 min-w-[160px]">
            <Field label="Name">
              <TextInput value={name} onChange={(e) => setName(e.target.value)} placeholder="ci-pipeline" />
            </Field>
          </div>
          <Field label="Role">
            <select
              className="rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
              value={role}
              onChange={(e) => setRole(e.target.value)}
            >
              {SA_ROLES.map((r) => (
                <option key={r} value={r}>
                  {ROLE_LABEL[r] ?? r}
                </option>
              ))}
            </select>
          </Field>
          <Button onClick={create} disabled={busy || !name.trim()}>
            Create
          </Button>
        </div>
        {err && <p className="mt-2 text-xs text-rose-600">{err}</p>}
      </Card>

      {rows === null ? (
        <Spinner />
      ) : rows.length === 0 ? (
        <EmptyState title="No service accounts" hint="Create one to issue an API key for automation." />
      ) : (
        <Card className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                <th className="px-4 py-2 text-left font-semibold">Name</th>
                <th className="px-4 py-2 text-left font-semibold">Role</th>
                <th className="px-4 py-2 text-left font-semibold">Key</th>
                <th className="px-4 py-2 text-left font-semibold">Created</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody>
              {rows.map((sa) => (
                <tr key={sa.id} className="border-b border-slate-100 last:border-0">
                  <td className="px-4 py-2 font-medium text-navy">{sa.name}</td>
                  <td className="px-4 py-2 text-slate-600">{ROLE_LABEL[sa.role] ?? sa.role}</td>
                  <td className="px-4 py-2 font-mono text-xs text-slate-500">{sa.key_prefix}…</td>
                  <td className="px-4 py-2 text-slate-400">
                    {sa.created_at ? new Date(sa.created_at).toLocaleDateString() : "—"}
                  </td>
                  <td className="px-4 py-2 text-right">
                    <Button variant="ghost" className="!px-2 !py-1 text-xs" onClick={() => rotate(sa)} disabled={busy}>
                      Rotate key
                    </Button>
                    <Button variant="ghost" className="!ml-1 !px-2 !py-1 text-xs" onClick={() => remove(sa)} disabled={busy}>
                      Delete
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}

export default function Access() {
  const [matrix, setMatrix] = useState<RoleMatrix | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.roleMatrix().then((r) => setMatrix(r.data)).catch((e) => setErr((e as Error).message));
  }, []);

  return (
    <div className="space-y-8">
      <div>
        <h2 className="text-xl font-bold text-navy">People &amp; access</h2>
        <p className="text-sm text-slate-500">Role permissions and service identities for this group.</p>
      </div>

      <section className="space-y-3">
        <h3 className="text-sm font-semibold text-navy">Role permission matrix</h3>
        {err && <p className="text-sm text-rose-600">{err}</p>}
        {matrix === null ? <Spinner /> : <RoleMatrixTable matrix={matrix} />}
      </section>

      <ServiceAccounts />
    </div>
  );
}
