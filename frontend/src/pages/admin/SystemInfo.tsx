import { useEffect, useState } from "react";
import { api, ApiError } from "../../api";
import type { Connection, SampleStore, SystemInfo as Info } from "../../types";
import { Button, Card, Field, Spinner, TextInput } from "../../components/ui";

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between border-b border-slate-100 px-4 py-2.5 last:border-0">
      <span className="text-sm text-slate-500">{label}</span>
      <span className="font-mono text-sm font-medium text-navy">{value}</span>
    </div>
  );
}

export default function SystemInfo() {
  const [info, setInfo] = useState<Info | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.systemInfo().then((r) => setInfo(r.data)).catch((e) => setErr((e as Error).message));
  }, []);

  if (err) return <p className="text-sm text-rose-600">{err}</p>;
  if (!info) return <Spinner />;

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-xl font-bold text-navy">System</h2>
        <p className="text-sm text-slate-500">
          Runtime configuration (read-only). Infrastructure is set via environment; editable
          runtime settings &amp; data-retention land in a later phase.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Card>
          <div className="border-b border-slate-100 px-4 py-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
            Runtime
          </div>
          <Row label="Version" value={info.version} />
          <Row label="Profile" value={info.profile} />
          <Row label="Identity provider" value={info.identity_provider} />
          <Row label="Metadata database" value={info.database} />
        </Card>

        <Card>
          <div className="border-b border-slate-100 px-4 py-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
            Backends &amp; scheduler
          </div>
          <Row label="Secret backend" value={info.secret_backend} />
          <Row label="Result backend" value={info.result_backend} />
          <Row label="Scheduler" value={info.scheduler.enabled ? "enabled" : "disabled"} />
          <Row label="Interval" value={`${info.scheduler.interval_seconds}s`} />
        </Card>
      </div>

      <SampleStoreCard />
    </div>
  );
}

function SampleStoreCard() {
  const [store, setStore] = useState<SampleStore | null>(null);
  const [missing, setMissing] = useState(false);
  const [conns, setConns] = useState<Connection[]>([]);
  const [connectionId, setConnectionId] = useState("");
  const [database, setDatabase] = useState("");
  const [schema, setSchema] = useState("");
  const [table, setTable] = useState("DIFF_SAMPLES");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .sampleStore()
      .then((r) => {
        setStore(r.data);
        if (r.data.connection_id) setConnectionId(r.data.connection_id);
        if (r.data.database) setDatabase(r.data.database);
        if (r.data.schema) setSchema(r.data.schema);
        if (r.data.table) setTable(r.data.table);
      })
      .catch((e: Error) => {
        if (e instanceof ApiError && e.status === 404) setMissing(true);
        else setErr(e.message);
      });
    api.listConnections().then((r) => setConns(r.data)).catch(() => undefined);
  }, []);

  const snowflake = conns.filter((c) => c.source_type === "snowflake");

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.setSampleStore({
        connection_id: connectionId,
        database,
        schema,
        table: table || "DIFF_SAMPLES",
      });
      setStore(r.data);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function clear() {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.clearSampleStore();
      setStore(r.data);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <div className="border-b border-slate-100 px-4 py-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
        Exception samples
      </div>
      {missing ? (
        <p className="px-4 py-3 text-sm text-slate-500">
          Exception rows from a compare can stay in your Snowflake account. This server has not
          loaded that setting yet.
        </p>
      ) : !store && !err ? (
        <div className="px-4 py-4">
          <Spinner />
        </div>
      ) : (
        <div className="space-y-3 px-4 py-4">
          <p className="text-sm text-slate-600">
            {store?.location
              ? `Exception rows are written to ${store.location} in your Snowflake account. This app keeps the counts and a pointer.`
              : "Exception rows from a compare are stored with this app. Point them at a table in your Snowflake account and the row values stay there."}
          </p>
          {snowflake.length === 0 ? (
            <p className="text-sm text-slate-500">Add a Snowflake connection first.</p>
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field label="Snowflake connection">
                <select
                  value={connectionId}
                  onChange={(e) => setConnectionId(e.target.value)}
                  className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700"
                >
                  <option value="">Choose</option>
                  {snowflake.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Database">
                <TextInput value={database} onChange={(e) => setDatabase(e.target.value)} placeholder="ANALYTICS" />
              </Field>
              <Field label="Schema">
                <TextInput value={schema} onChange={(e) => setSchema(e.target.value)} placeholder="DVP" />
              </Field>
              <Field label="Table">
                <TextInput value={table} onChange={(e) => setTable(e.target.value)} placeholder="DIFF_SAMPLES" />
              </Field>
            </div>
          )}
          {err && <p className="text-sm text-rose-600">{err}</p>}
          {snowflake.length > 0 && (
            <div className="flex gap-2">
              <Button disabled={busy || !connectionId || !database || !schema} onClick={save}>
                {busy ? "Saving…" : "Save location"}
              </Button>
              {store?.location && (
                <Button variant="ghost" disabled={busy} onClick={clear}>
                  Keep samples in this app
                </Button>
              )}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
