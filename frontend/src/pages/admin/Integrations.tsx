import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../api";
import type { Integration, IntegrationSpecs, NotificationRoute, SystemInfo } from "../../types";
import { Button, Card, EmptyState, Field, HealthBadge, Spinner, TextInput } from "../../components/ui";
import { Combobox } from "../../components/Combobox";
import { SortTh, useSort } from "../../components/sortable";

const statusTone = (s: string) =>
  s === "active" ? "healthy" : s === "error" ? "critical" : "unknown";

export default function Integrations() {
  const [specs, setSpecs] = useState<IntegrationSpecs | null>(null);
  const [sys, setSys] = useState<SystemInfo | null>(null);
  const [channels, setChannels] = useState<Integration[]>([]);
  const [routes, setRoutes] = useState<NotificationRoute[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [specsErr, setSpecsErr] = useState<string | null>(null);

  // add-channel form
  const [provider, setProvider] = useState("slack");
  const [name, setName] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [testResult, setTestResult] = useState<Record<string, string>>({});

  // add-route form
  const [routeInteg, setRouteInteg] = useState("");
  const [routeSev, setRouteSev] = useState("warning");

  const reload = useCallback(() => {
    Promise.all([api.listIntegrations(), api.listRoutes()])
      .then(([i, r]) => {
        setChannels(i.data);
        setRoutes(r.data);
      })
      .catch((e) => setErr((e as Error).message))
      .finally(() => setLoaded(true));
  }, []);

  useEffect(() => {
    api.integrationSpecs().then((r) => setSpecs(r.data)).catch((e) => setSpecsErr((e as Error).message));
    api.systemInfo().then((r) => setSys(r.data)).catch(() => undefined);
    reload();
  }, [reload]);

  const spec = useMemo(
    () => specs?.providers.find((p) => p.provider === provider),
    [specs, provider]
  );

  const add = async () => {
    setBusy(true);
    setErr(null);
    try {
      await api.createIntegration({ provider, name, values });
      setName("");
      setValues({});
      reload();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (c: Integration) => {
    await api.updateIntegration(c.id, { enabled: !c.enabled }).catch((e) => setErr((e as Error).message));
    reload();
  };
  const test = async (c: Integration) => {
    const r = await api.testIntegration(c.id).catch((e) => ({ data: { ok: false, detail: (e as Error).message } }));
    setTestResult((m) => ({ ...m, [c.id]: `${r.data.ok ? "✓" : "✗"} ${r.data.detail}` }));
    reload();
  };
  const remove = async (c: Integration) => {
    if (!window.confirm(`Delete integration "${c.name}"? Its routes are removed too.`)) return;
    await api.deleteIntegration(c.id).catch((e) => setErr((e as Error).message));
    reload();
  };

  const addRoute = async () => {
    if (!routeInteg) return;
    setErr(null);
    try {
      await api.createRoute({ event: "incident_opened", min_severity: routeSev, integration_id: routeInteg });
      reload();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  const removeRoute = async (r: NotificationRoute) => {
    await api.deleteRoute(r.id).catch((e) => setErr((e as Error).message));
    reload();
  };

  const integName = (id: string) => channels.find((c) => c.id === id)?.name ?? id.slice(0, 8);
  const { sorted: sortedChannels, sort, requestSort } = useSort(channels, {
    channel: (c) => c.name,
    status: (c) => (c.enabled ? c.status : "disabled"),
    last_delivery: (c) => c.last_delivery_at,
  });

  if (!specs) {
    if (!loaded && !specsErr) return <Spinner />;
    return <p className="text-sm text-rose-600">{specsErr || "Could not load channels."}</p>;
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-bold text-navy">Integrations</h2>
        <p className="text-sm text-slate-500">
          A new incident at or above the severity you choose pages the channel once.
          Later updates stay on that same page.
        </p>
      </div>

      {sys?.is_local && (
        <div className="rounded-lg border border-sky-200 bg-sky-50 px-4 py-3 text-sm text-sky-800">
          In <b>local mode</b>, deliveries are <b>recorded</b> (shown below) rather than sent
          externally — so you can configure and test channels without live credentials.
        </div>
      )}
      {err && <p className="text-sm text-rose-600">{err}</p>}

      {/* Add a channel */}
      <Card className="space-y-4 p-4">
        <div className="text-sm font-semibold text-navy">Add a channel</div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="Provider">
            <Combobox
              value={provider}
              onChange={(v) => {
                setProvider(v);
                setValues({});
              }}
              allowCustom={false}
              options={specs.providers.map((p) => ({ value: p.provider, label: p.label }))}
            />
          </Field>
          <Field label="Name">
            <TextInput value={name} onChange={(e) => setName(e.target.value)} placeholder="#data-alerts" />
          </Field>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {spec?.fields.map((f) => (
            <Field key={f.key} label={f.label + (f.secret ? " (secret)" : "")}>
              <TextInput
                type={f.input === "password" ? "password" : f.input === "number" ? "number" : "text"}
                value={values[f.key] ?? ""}
                onChange={(e) => setValues((m) => ({ ...m, [f.key]: e.target.value }))}
              />
            </Field>
          ))}
        </div>
        <div className="flex justify-end">
          <Button onClick={add} disabled={busy || !name}>
            {busy ? "Adding…" : "Add channel"}
          </Button>
        </div>
      </Card>

      {/* Channels */}
      {channels.length === 0 ? (
        <EmptyState
          title="No channels yet"
          hint="Add Slack, PagerDuty, email, Teams, or a webhook above."
        />
      ) : (
        <Card className="overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                <SortTh label="Channel" columnKey="channel" sort={sort} onSort={requestSort} className="px-4 py-2 font-semibold" />
                <SortTh label="Status" columnKey="status" sort={sort} onSort={requestSort} className="px-4 py-2 font-semibold" />
                <SortTh label="Last delivery" columnKey="last_delivery" sort={sort} onSort={requestSort} className="px-4 py-2 font-semibold" />
                <th className="px-4 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {sortedChannels.map((c) => (
                <tr key={c.id} className="border-b border-slate-100 align-top last:border-0">
                  <td className="px-4 py-2">
                    <div className="font-medium text-navy">{c.name}</div>
                    <div className="text-xs text-slate-400">{c.provider}{c.has_secret ? " · secret set" : ""}</div>
                    {testResult[c.id] && <div className="mt-1 text-xs text-slate-500">{testResult[c.id]}</div>}
                    {c.last_error && <div className="mt-1 text-xs text-rose-500">{c.last_error}</div>}
                  </td>
                  <td className="px-4 py-2">
                    <HealthBadge status={statusTone(c.status)} label={c.enabled ? c.status : "disabled"} />
                  </td>
                  <td className="px-4 py-2 text-slate-500">
                    {c.last_delivery_at ? new Date(c.last_delivery_at).toLocaleString() : "—"}
                  </td>
                  <td className="px-4 py-2 text-right">
                    <div className="flex justify-end gap-3 text-xs font-medium">
                      <button onClick={() => test(c)} className="text-teal-700 hover:underline">Test</button>
                      <button onClick={() => toggle(c)} className="text-slate-500 hover:underline">
                        {c.enabled ? "Disable" : "Enable"}
                      </button>
                      <button onClick={() => remove(c)} className="text-slate-400 hover:text-rose-600">Delete</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {/* Routing rules */}
      <div>
        <div className="mb-2 text-sm font-semibold text-navy">Incident routing</div>
        <Card className="space-y-3 p-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr,180px,auto] sm:items-end">
            <Field label="When an incident opens, notify">
              <Combobox
                value={routeInteg}
                onChange={setRouteInteg}
                allowCustom={false}
                placeholder="select a channel"
                options={channels.map((c) => ({ value: c.id, label: `${c.name} (${c.provider})` }))}
              />
            </Field>
            <Field label="At or above severity">
              <Combobox
                value={routeSev}
                onChange={setRouteSev}
                allowCustom={false}
                options={specs.severities.map((s) => ({ value: s, label: s }))}
              />
            </Field>
            <Button onClick={addRoute} disabled={!routeInteg}>Add rule</Button>
          </div>

          {routes.length === 0 ? (
            <p className="text-xs text-slate-400">No routing rules — incidents won't notify anyone yet.</p>
          ) : (
            <ul className="divide-y divide-slate-100">
              {routes.map((r) => (
                <li key={r.id} className="flex items-center justify-between py-2 text-sm">
                  <span className="text-slate-600">
                    <span className="font-medium text-navy">{r.event.replace("_", " ")}</span>
                    {" ≥ "}
                    <span className="font-medium">{r.min_severity}</span>
                    {" → "}
                    {integName(r.integration_id)}
                  </span>
                  <button onClick={() => removeRoute(r)} className="text-xs text-slate-400 hover:text-rose-600">
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}
