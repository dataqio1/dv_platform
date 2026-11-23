import { useCallback, useEffect, useState } from "react";
import { usePrefs } from "../../prefs";
import { api } from "../../api";
import type { Member } from "../../types";
import { Button, Card, EmptyState, Field, Spinner, TextInput } from "../../components/ui";
import { Combobox } from "../../components/Combobox";
import { SortTh, useSort } from "../../components/sortable";

// Roles an admin can assign within a group (org_admin is granted at the org level).
const ROLES = [
  { value: "report_viewer", label: "Report viewer" },
  { value: "viewer", label: "Viewer" },
  { value: "editor", label: "Editor" },
  { value: "group_admin", label: "Group admin" },
];
const roleLabel = (r: string) => ROLES.find((x) => x.value === r)?.label ?? r;

export default function Members() {
  const { groupId } = usePrefs();
  const [members, setMembers] = useState<Member[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("viewer");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    if (!groupId) return;
    api.listMembers(groupId).then((r) => setMembers(r.data)).catch((e) => setErr((e as Error).message));
  }, [groupId]);
  useEffect(load, [load]);

  const ldap = (members ?? []).some((m) => m.ldap_managed);

  const add = async () => {
    setBusy(true);
    setErr(null);
    try {
      await api.addMember(groupId, email, role);
      setEmail("");
      load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const changeRole = async (m: Member, next: string) => {
    if (next === m.role) return;
    setErr(null);
    try {
      await api.updateMemberRole(groupId, m.user_id, next);
      load();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  const remove = async (m: Member) => {
    if (!window.confirm(`Remove ${m.email} from this group?`)) return;
    setErr(null);
    try {
      await api.removeMember(groupId, m.user_id);
      load();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const { sorted: sortedMembers, sort, requestSort } = useSort(members ?? [], {
    member: (m) => m.display_name || m.email,
    role: (m) => m.role,
    joined: (m) => m.joined_at,
  });

  if (members === null) return <Spinner />;

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-xl font-bold text-navy">Members &amp; Roles</h2>
        <p className="text-sm text-slate-500">Who can access this group, and what they can do.</p>
      </div>

      {ldap && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          Membership for this group is <b>managed by LDAP</b> and is read-only here. Change access
          via the directory group.
        </div>
      )}

      {!ldap && (
        <Card className="p-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr,200px,auto] sm:items-end">
            <Field label="Add member (email)">
              <TextInput
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="analyst@acme.io"
              />
            </Field>
            <Field label="Role">
              <Combobox value={role} onChange={setRole} allowCustom={false} options={ROLES} />
            </Field>
            <Button onClick={add} disabled={busy || !email}>
              {busy ? "Adding…" : "Add"}
            </Button>
          </div>
        </Card>
      )}

      {err && <p className="text-sm text-rose-600">{err}</p>}

      {members.length === 0 ? (
        <EmptyState title="No members yet" hint="Add someone above to grant access." />
      ) : (
        <Card className="overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                <SortTh label="Member" columnKey="member" sort={sort} onSort={requestSort} className="px-4 py-2 font-semibold" />
                <SortTh label="Role" columnKey="role" sort={sort} onSort={requestSort} className="px-4 py-2 font-semibold" />
                <SortTh label="Joined" columnKey="joined" sort={sort} onSort={requestSort} className="px-4 py-2 font-semibold" />
                <th className="px-4 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {sortedMembers.map((m) => (
                <tr key={m.user_id} className="border-b border-slate-100 last:border-0">
                  <td className="px-4 py-2">
                    <div className="font-medium text-navy">{m.display_name || m.email}</div>
                    {m.display_name && <div className="text-xs text-slate-400">{m.email}</div>}
                  </td>
                  <td className="px-4 py-2">
                    {ldap ? (
                      <span className="text-slate-600">{roleLabel(m.role)}</span>
                    ) : (
                      <div className="w-44">
                        <Combobox
                          value={m.role}
                          onChange={(v) => changeRole(m, v)}
                          allowCustom={false}
                          options={ROLES}
                        />
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-2 text-slate-500">
                    {m.joined_at ? new Date(m.joined_at).toLocaleDateString() : "—"}
                  </td>
                  <td className="px-4 py-2 text-right">
                    {!ldap && (
                      <button
                        onClick={() => remove(m)}
                        className="text-xs font-medium text-slate-400 hover:text-rose-600"
                      >
                        Remove
                      </button>
                    )}
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
