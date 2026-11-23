import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../api";
import type { MetadataConvention } from "../../types";
import { Button, Card, EmptyState, Field, Spinner, TextInput } from "../../components/ui";

// The standard audit/metadata roles most ETL tools stamp into every table. Admins can add
// arbitrary roles too — these are just convenient starting rows + a datalist of suggestions.
const COMMON_ROLES = [
  "loaded_at",
  "updated_at",
  "batch_id",
  "source_system",
  "operation",
];

type RolePair = { role: string; column: string };
type Scd2 = { start: string; end: string; current: string };

const EMPTY_SCD2: Scd2 = { start: "", end: "", current: "" };
const blankRows = (): RolePair[] => COMMON_ROLES.map((role) => ({ role, column: "" }));

export default function EtlTools() {
  const [list, setList] = useState<MetadataConvention[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // create / edit form
  const [editingId, setEditingId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [rows, setRows] = useState<RolePair[]>(blankRows());
  const [scd2, setScd2] = useState<Scd2>(EMPTY_SCD2);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(() => {
    api
      .listMetadataConventions()
      .then((r) => setList(r.data))
      .catch((e) => setErr((e as Error).message));
  }, []);
  useEffect(reload, [reload]);

  const system = useMemo(() => (list ?? []).filter((c) => c.system), [list]);
  const custom = useMemo(() => (list ?? []).filter((c) => !c.system), [list]);

  const resetForm = () => {
    setEditingId(null);
    setName("");
    setRows(blankRows());
    setScd2(EMPTY_SCD2);
    setErr(null);
  };

  const startEdit = (c: MetadataConvention) => {
    setEditingId(c.id);
    setName(c.name);
    const entries = Object.entries(c.roles ?? {}).map(([role, column]) => ({ role, column: String(column) }));
    setRows(entries.length ? entries : blankRows());
    const s = (c.scd2 ?? {}) as Record<string, string>;
    setScd2({ start: s.start ?? "", end: s.end ?? "", current: s.current ?? "" });
    setErr(null);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const setRow = (i: number, patch: Partial<RolePair>) =>
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const addRow = () => setRows((rs) => [...rs, { role: "", column: "" }]);
  const removeRow = (i: number) => setRows((rs) => rs.filter((_, j) => j !== i));

  const buildBody = () => {
    const roles: Record<string, string> = {};
    for (const { role, column } of rows) {
      const k = role.trim();
      const v = column.trim();
      if (k && v) roles[k] = v;
    }
    const s: Record<string, string> = {};
    if (scd2.start.trim()) s.start = scd2.start.trim();
    if (scd2.end.trim()) s.end = scd2.end.trim();
    if (scd2.current.trim()) s.current = scd2.current.trim();
    return { name: name.trim(), roles, scd2: Object.keys(s).length ? s : null };
  };

  const save = async () => {
    const body = buildBody();
    if (!body.name) return setErr("Give the ETL tool a name.");
    if (Object.keys(body.roles).length === 0)
      return setErr("Map at least one metadata column (role → column).");
    setBusy(true);
    setErr(null);
    try {
      if (editingId) await api.updateMetadataConvention(editingId, body);
      else await api.createMetadataConvention(body);
      resetForm();
      reload();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (c: MetadataConvention) => {
    if (!confirm(`Delete ETL tool "${c.name}"? Monitors/connections using it keep their stored columns.`)) return;
    try {
      await api.deleteMetadataConvention(c.id);
      if (editingId === c.id) resetForm();
      reload();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const roleChips = (c: MetadataConvention) => {
    const entries = Object.entries(c.roles ?? {});
    if (!entries.length) return <span className="text-slate-400">—</span>;
    return (
      <div className="flex flex-wrap gap-1.5">
        {entries.map(([r, col]) => (
          <span key={r} className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-600">
            {r} → <span className="font-medium text-navy">{String(col)}</span>
          </span>
        ))}
      </div>
    );
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-bold text-navy">ETL Tools</h1>
        <p className="mt-1 max-w-2xl text-sm text-slate-500">
          Define a custom ETL tool as a named set of metadata/audit columns (role → column) that your
          pipelines stamp into every table — load time, batch id, SCD2 dates, etc. Tools defined here appear
          in the monitor wizard's <span className="font-medium">ETL tool</span> dropdown, so you declare the
          audit columns once and reuse them everywhere.
        </p>
      </div>

      {err && <div className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700">{err}</div>}

      {/* Create / edit form */}
      <Card className="space-y-4 p-6">
        <h2 className="text-sm font-bold uppercase tracking-wide text-navy">
          {editingId ? "Edit ETL tool" : "Define a custom ETL tool"}
        </h2>

        <Field label="Tool name" hint="e.g. IDMC-CDC, Fivetran, S3-Loader">
          <TextInput value={name} onChange={(e) => setName(e.target.value)} placeholder="My ETL tool" />
        </Field>

        <div>
          <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
            Metadata columns (role → column name)
          </div>
          <datalist id="role-suggestions">
            {COMMON_ROLES.map((r) => (
              <option key={r} value={r} />
            ))}
          </datalist>
          <div className="space-y-2">
            {rows.map((r, i) => (
              <div key={i} className="flex items-center gap-2">
                <input
                  list="role-suggestions"
                  value={r.role}
                  onChange={(e) => setRow(i, { role: e.target.value })}
                  placeholder="role (e.g. loaded_at)"
                  className="w-1/2 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
                />
                <span className="text-slate-400">→</span>
                <input
                  value={r.column}
                  onChange={(e) => setRow(i, { column: e.target.value })}
                  placeholder="column (e.g. DW_LOAD_TS)"
                  className="w-1/2 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
                />
                <button
                  type="button"
                  onClick={() => removeRow(i)}
                  className="px-2 text-slate-400 hover:text-rose-600"
                  title="Remove row"
                >
                  ✕
                </button>
              </div>
            ))}
          </div>
          <Button variant="ghost" onClick={addRow} className="mt-2">
            + Add column
          </Button>
        </div>

        <Field label="SCD2 columns (optional)" hint="Slowly-changing-dimension effective dates & current flag.">
          <div className="flex flex-wrap gap-2">
            <TextInput value={scd2.start} onChange={(e) => setScd2({ ...scd2, start: e.target.value })} placeholder="start (EFF_FROM)" />
            <TextInput value={scd2.end} onChange={(e) => setScd2({ ...scd2, end: e.target.value })} placeholder="end (EFF_TO)" />
            <TextInput value={scd2.current} onChange={(e) => setScd2({ ...scd2, current: e.target.value })} placeholder="current (IS_CURRENT)" />
          </div>
        </Field>

        <div className="flex gap-2">
          <Button onClick={save} disabled={busy}>
            {busy ? "Saving…" : editingId ? "Save changes" : "Create ETL tool"}
          </Button>
          {editingId && (
            <Button variant="ghost" onClick={resetForm} disabled={busy}>
              Cancel
            </Button>
          )}
        </div>
      </Card>

      {/* Custom tools */}
      <Card className="p-6">
        <h2 className="mb-3 text-sm font-bold uppercase tracking-wide text-navy">Your ETL tools</h2>
        {list === null ? (
          <Spinner label="Loading…" />
        ) : custom.length === 0 ? (
          <EmptyState title="No custom ETL tools yet" hint="Define one above to make it selectable in the monitor wizard." />
        ) : (
          <div className="divide-y divide-slate-100">
            {custom.map((c) => (
              <div key={c.id} className="flex items-start justify-between gap-4 py-3">
                <div className="min-w-0">
                  <div className="font-semibold text-navy">{c.name}</div>
                  <div className="mt-1">{roleChips(c)}</div>
                </div>
                <div className="flex shrink-0 gap-2">
                  <Button variant="ghost" onClick={() => startEdit(c)}>Edit</Button>
                  <Button variant="ghost" onClick={() => remove(c)}>Delete</Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Built-in presets (read-only) */}
      {system.length > 0 && (
        <Card className="p-6">
          <h2 className="mb-1 text-sm font-bold uppercase tracking-wide text-navy">Built-in presets</h2>
          <p className="mb-3 text-xs text-slate-400">Shipped conventions, always available in the dropdown. Read-only.</p>
          <div className="divide-y divide-slate-100">
            {system.map((c) => (
              <div key={c.id} className="flex items-start justify-between gap-4 py-3">
                <div className="min-w-0">
                  <div className="font-semibold text-navy">
                    {c.name} <span className="ml-1 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-slate-500">built-in</span>
                  </div>
                  <div className="mt-1">{roleChips(c)}</div>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
