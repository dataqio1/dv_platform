import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { Button, Card, EmptyState, Field, Spinner, TextInput } from "../components/ui";
import { patternToLoadType } from "../components/TableLoadConfig";
import type { Connection, WatermarkSuggestion } from "../types";

const LOAD_LABEL: Record<string, string> = {
  incremental: "Incremental",
  full_reload: "Full reload",
  append: "Append",
};

const SELECT_ALL_LIMIT = 30;

export default function ConfirmWatch() {
  const { connectionId = "" } = useParams();
  const nav = useNavigate();
  const [connection, setConnection] = useState<Connection | null>(null);
  const [database, setDatabase] = useState("");
  const [schema, setSchema] = useState("");
  const [schemas, setSchemas] = useState<string[]>([]);
  const [tables, setTables] = useState<string[] | null>(null);
  const [kept, setKept] = useState<string[]>([]);
  const [name, setName] = useState("");
  const [suggestion, setSuggestion] = useState<WatermarkSuggestion | null>(null);
  const [loadPattern, setLoadPattern] = useState<string | null>(null);
  const [suggesting, setSuggesting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setErr(null);
    setTables(null);
    api
      .getConnection(connectionId)
      .then(async (r) => {
        if (cancelled) return;
        const conn = r.data;
        setConnection(conn);
        const db = String(conn.coordinates.database ?? "");
        const preset = String(conn.coordinates.schema ?? "");
        setDatabase(db);
        setName(preset ? `${preset} on ${conn.name}` : conn.name);
        if (preset) {
          setSchema(preset);
          return;
        }
        const listed = await api.listSchemas(connectionId, db || undefined);
        if (cancelled) return;
        const names = listed.data.schemas ?? [];
        setSchemas(names);
        if (names.length === 1) setSchema(names[0]);
      })
      .catch((e) => {
        if (!cancelled) setErr((e as Error).message);
      });
    return () => {
      cancelled = true;
    };
  }, [connectionId]);

  useEffect(() => {
    if (!schema) return;
    let cancelled = false;
    setTables(null);
    setKept([]);
    api
      .listCatalogTables(connectionId, schema, database || undefined)
      .then((r) => {
        if (cancelled) return;
        const names = (r.data.tables ?? []).map((t) => t.name);
        setTables(names);
        setKept(names.length <= SELECT_ALL_LIMIT ? names : []);
      })
      .catch((e) => {
        if (!cancelled) {
          setTables([]);
          setErr((e as Error).message);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [connectionId, database, schema]);

  useEffect(() => {
    if (!schema || kept.length === 0) {
      setSuggestion(null);
      setLoadPattern(null);
      return;
    }
    let cancelled = false;
    setSuggesting(true);
    const scope = { database: database || undefined, schema, tables: kept };
    const first = kept[0];
    Promise.all([
      api.detectWatermark(connectionId, scope).catch(() => null),
      api
        .listColumns(connectionId, schema, first, database || undefined)
        .then((cols) =>
          api.classifyPattern(cols.data.columns.map((c) => ({ name: c.name, data_type: c.data_type }))),
        )
        .catch(() => null),
    ]).then(([detected, classified]) => {
      if (cancelled) return;
      setSuggestion(detected?.data.suggested ?? null);
      const pattern = (classified?.data.load_pattern as string | undefined) ?? null;
      setLoadPattern(pattern);
      setSuggesting(false);
    });
    return () => {
      cancelled = true;
    };
  }, [connectionId, database, schema, kept]);

  function toggle(table: string) {
    setKept((prev) => (prev.includes(table) ? prev.filter((t) => t !== table) : [...prev, table]));
  }

  const watermark =
    suggestion && suggestion.covers_all_tables ? suggestion : null;
  const loadType = patternToLoadType(loadPattern ?? (watermark ? "incremental" : "unknown"));

  async function confirm() {
    if (!connection || kept.length === 0) return;
    setBusy(true);
    setErr(null);
    try {
      const mon = await api.createMonitor({
        connection_id: connection.id,
        name: name.trim() || connection.name,
        scope: { database: database || undefined, schema, tables: kept },
        watermark_column: watermark?.column ?? null,
        watermark_type: watermark?.type ?? null,
        load_type: loadType,
      });
      for (let i = 0; i < kept.length; i++) {
        const ref = [database, schema, kept[i]].filter(Boolean).join(".");
        setProgress(`Profiling ${i + 1} of ${kept.length}`);
        await api.profileTable(mon.data.id, ref).catch(() => undefined);
      }
      nav(`/observability/${mon.data.id}`);
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
      setProgress(null);
    }
  }

  if (err && !connection) return <p className="text-sm text-rose-600">{err}</p>;
  if (!connection) return <Spinner />;

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div>
        <Link to="/admin/connections" className="text-sm text-slate-400 hover:text-slate-600">
          ← Connections
        </Link>
        <h1 className="mt-1 text-2xl font-bold text-navy">Confirm tables to watch</h1>
        <p className="mt-1 text-sm text-slate-500">
          {`${[connection.name, [database, schema].filter(Boolean).join(".")].filter(Boolean).join(" · ")}. Uncheck any table you do not want watched.`}
        </p>
      </div>

      {!schema && (
        <Card className="space-y-3 p-5">
          <Field label="Schema">
            {schemas.length === 0 ? (
              <Spinner label="Loading schemas…" />
            ) : (
              <div className="flex flex-wrap gap-2">
                {schemas.map((item) => (
                  <button
                    key={item}
                    type="button"
                    onClick={() => setSchema(item)}
                    className="rounded-full bg-slate-100 px-3 py-1 text-sm text-slate-700 hover:bg-slate-200"
                  >
                    {item}
                  </button>
                ))}
              </div>
            )}
          </Field>
        </Card>
      )}

      {schema && tables === null && <Spinner label="Loading tables…" />}

      {schema && tables && tables.length === 0 && (
        <EmptyState title="No tables in this schema" hint="Pick another schema, or check the connection." />
      )}

      {schema && tables && tables.length > 0 && (
        <>
          <Card className="space-y-2 p-5">
            <h2 className="text-sm font-semibold text-navy">Suggestion</h2>
            {suggesting ? (
              <Spinner label="Reading columns…" />
            ) : (
              <>
                <p className="text-sm text-slate-700">
                  {watermark
                    ? `Watermark ${watermark.column} (${watermark.type}) is on every selected table.`
                    : suggestion
                      ? `Watermark ${suggestion.column} is on ${suggestion.present_in} of ${suggestion.total_tables} selected tables, so it will not be set for the monitor.`
                      : "No watermark column stood out."}
                </p>
                <p className="text-sm text-slate-700">
                  {`Load pattern ${LOAD_LABEL[loadType] ?? loadType}${loadPattern ? ` · classified from ${kept[0]}` : ""}.`}
                </p>
              </>
            )}
          </Card>

          <Card className="divide-y divide-slate-100">
            {tables.map((table) => {
              const on = kept.includes(table);
              return (
                <label key={table} className="flex items-center gap-3 px-5 py-3 text-sm">
                  <input type="checkbox" checked={on} onChange={() => toggle(table)} />
                  <span className={on ? "font-medium text-navy" : "text-slate-400"}>{table}</span>
                </label>
              );
            })}
          </Card>

          {tables.length > SELECT_ALL_LIMIT && kept.length === 0 && (
            <p className="text-sm text-slate-500">
              {tables.length} tables. Check the ones to watch. Nothing is profiled until you confirm.
            </p>
          )}

          <Field label="Monitor name">
            <TextInput value={name} onChange={(e) => setName(e.target.value)} />
          </Field>

          {err && <p className="text-sm text-rose-600">{err}</p>}
          {progress && <p className="text-sm text-slate-500">{progress}</p>}

          <div className="flex justify-end">
            <Button onClick={confirm} disabled={busy || kept.length === 0 || !name.trim()}>
              {busy ? "Creating…" : `Watch ${kept.length} table${kept.length === 1 ? "" : "s"}`}
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
