import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import type { Connection, TableColumn, TableLoadConfig } from "../types";
import { Field, Spinner } from "../components/ui";
import { Button, Card, Icon, Pill, Stepper } from "../components/vex";
import {
  CheckCount,
  ConfigureChecks,
  PerTableOverrides,
  DEFAULT_CHECKS,
  alertPolicyToChecks,
  checksToAlertPolicy,
  type ChecksConfig,
  type FreshnessSuggestion,
  type PolicyOverride,
} from "../components/ConfigureChecks";
import { Combobox, TablePicker } from "../components/Combobox";
import { TableLoadConfigRow, BulkApplyPanel, patternToLoadType, type PatternSpecs } from "../components/TableLoadConfig";
import { LoadTypeDetectionTable } from "../components/LoadTypeDetectionTable";

type CatalogTable = { name: string; type: string };
const STEPS = ["Select data", "Configure checks", "Review"];

const cronToSchedule = (cron: string | null | undefined): string =>
  !cron ? "manual" : cron.startsWith("0 * ") ? "hourly" : "daily";

const cronToTime = (cron: string | null | undefined): string => {
  const p = (cron ?? "").split(/\s+/);
  if (p.length >= 2 && p[1] !== "*" && p[1] !== "") {
    const hh = Number(p[1]), mm = Number(p[0]);
    if (Number.isFinite(hh) && Number.isFinite(mm)) return `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
  }
  return "06:00";
};

// Build a cron from the Configure-checks schedule + daily time (UTC).
const checksToCron = (c: ChecksConfig): string | null => {
  if (c.schedule === "manual") return null;
  if (c.schedule === "hourly") return "0 * * * *";
  const [hh, mm] = (c.scheduleTime || "06:00").split(":");
  return `${Number(mm)} ${Number(hh)} * * *`;
};

export default function CreateMonitor() {
  const nav = useNavigate();
  const { monitorId } = useParams();
  const editMode = Boolean(monitorId);
  const [editLoaded, setEditLoaded] = useState(!editMode);
  const [step, setStep] = useState(0);
  const [connections, setConnections] = useState<Connection[] | null>(null);
  const [connectionId, setConnectionId] = useState("");
  const [name, setName] = useState("New monitor");
  const [layer, setLayer] = useState("Source");

  const [databases, setDatabases] = useState<string[]>([]);
  const [database, setDatabase] = useState("");
  const [loadingDbs, setLoadingDbs] = useState(false);
  const [dbWarn, setDbWarn] = useState<string | null>(null);

  const [schemas, setSchemas] = useState<string[]>([]);
  const [schema, setSchema] = useState("PUBLIC");
  const [loadingSchemas, setLoadingSchemas] = useState(false);
  const [schemaWarn, setSchemaWarn] = useState<string | null>(null);

  const [catalogTables, setCatalogTables] = useState<CatalogTable[]>([]);
  const [loadingTables, setLoadingTables] = useState(false);
  const [tablesWarn, setTablesWarn] = useState<string | null>(null);
  const [tables, setTables] = useState<string[]>([]);

  const [specs, setSpecs] = useState<PatternSpecs | null>(null);
  const [configs, setConfigs] = useState<Record<string, TableLoadConfig>>({});
  const [tableCols, setTableCols] = useState<Record<string, TableColumn[]>>({});
  const [loadingCols, setLoadingCols] = useState<Record<string, boolean>>({});
  const [detecting, setDetecting] = useState<Record<string, boolean>>({});
  const [detectingAll, setDetectingAll] = useState(false);
  // Bulk-apply: which tables are checked + the pattern/fields to stamp onto them at once.
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [bulkCfg, setBulkCfg] = useState<TableLoadConfig>({ load_pattern: "unknown" });

  // ETL metadata-column convention (the "first step" — declare the audit columns once).
  const [presets, setPresets] = useState<Record<string, { label: string; roles: Record<string, string> }>>({});
  const [metadataPreset, setMetadataPreset] = useState("");

  // Auto-create suggested DQ rules on create, from a sample of each table's last batch.
  const [genRules, setGenRules] = useState(true);
  const [genSampleSize, setGenSampleSize] = useState(1000);
  const [noGenTables, setNoGenTables] = useState<Set<string>>(new Set());  // tables the user unchecked

  const [lookbackDays, setLookbackDays] = useState(30);
  const [checks, setChecks] = useState<ChecksConfig>(DEFAULT_CHECKS);
  // Per-table check overrides (threshold exceptions); the monitor policy is the default.
  const [overrides, setOverrides] = useState<Record<string, PolicyOverride>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.listConnections().then((r) => {
      setConnections(r.data);
      if (r.data[0] && !editMode) setConnectionId(r.data[0].id);
    });
    api.patternSpecs().then((r) => setSpecs(r.data));
    // The ETL-tool dropdown draws from the full convention library — shipped `sys:` presets AND
    // admin-defined custom ETL tools (Admin → ETL Tools), keyed by convention id.
    api.listMetadataConventions()
      .then((r) =>
        setPresets(
          Object.fromEntries(
            r.data.map((c) => [c.id, { label: c.system ? c.name : `${c.name} · custom`, roles: c.roles }]),
          ),
        ),
      )
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Edit mode: load the monitor and seed every field from its stored config (same wizard).
  useEffect(() => {
    if (!editMode || !monitorId) return;
    api.getMonitor(monitorId).then((r) => {
      const m = r.data;
      setName(m.name);
      setConnectionId(m.connection_id);
      setDatabase(m.scope?.database ?? "");
      setSchema(m.scope?.schema ?? "");
      setTables(m.scope?.tables ?? []);
      setLookbackDays(m.lookback_days ?? 30);
      if (m.metadata_convention_ids?.[0]) setMetadataPreset(m.metadata_convention_ids[0]);
      setChecks({
        ...alertPolicyToChecks(m.alert_policy as unknown as Record<string, unknown>),
        schedule: cronToSchedule(m.schedule_cron),
        scheduleTime: cronToTime(m.schedule_cron),
      });
      setEditLoaded(true);
    }).catch((e) => { setErr((e as Error).message); setEditLoaded(true); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [monitorId]);

  const selected = connections?.find((c) => c.id === connectionId);

  useEffect(() => {
    if (editMode) return;
    if (!connectionId) return;
    const coords = (selected?.coordinates ?? {}) as { database?: string; schema?: string };
    let ignore = false;
    setLoadingDbs(true); setDbWarn(null); setDatabases([]); setSchemas([]); setCatalogTables([]); setTables([]);
    api.listDatabases(connectionId).then((r) => {
      if (ignore) return;
      const dbs = r.data.databases ?? [];
      setDatabases(dbs); setDbWarn(r.warnings[0]?.message ?? null);
      setDatabase(coords.database && dbs.includes(coords.database) ? coords.database : dbs[0] ?? coords.database ?? "");
      if (coords.schema) setSchema(coords.schema);
    }).catch((e) => !ignore && setDbWarn((e as Error).message)).finally(() => !ignore && setLoadingDbs(false));
    return () => { ignore = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionId]);

  useEffect(() => {
    if (editMode) return;
    if (!connectionId || !database) return;
    const coords = (selected?.coordinates ?? {}) as { schema?: string };
    let ignore = false;
    setLoadingSchemas(true); setSchemaWarn(null); setCatalogTables([]);
    api.listSchemas(connectionId, database).then((r) => {
      if (ignore) return;
      const sc = r.data.schemas ?? [];
      setSchemas(sc); setSchemaWarn(r.warnings[0]?.message ?? null);
      const pref = coords.schema && sc.includes(coords.schema) ? coords.schema : sc[0] ?? "";
      if (pref) setSchema(pref);
    }).catch((e) => !ignore && setSchemaWarn((e as Error).message)).finally(() => !ignore && setLoadingSchemas(false));
    return () => { ignore = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionId, database]);

  useEffect(() => {
    if (editMode) return;
    if (!connectionId || !schema) return;
    let ignore = false;
    setLoadingTables(true); setTablesWarn(null); setTables([]); setConfigs({}); setTableCols({});
    api.listCatalogTables(connectionId, schema, database || undefined).then((r) => {
      if (ignore) return;
      setCatalogTables(r.data.tables ?? []); setTablesWarn(r.warnings[0]?.message ?? null);
    }).catch((e) => !ignore && setTablesWarn((e as Error).message)).finally(() => !ignore && setLoadingTables(false));
    return () => { ignore = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionId, database, schema]);

  const fetchCols = async (t: string): Promise<TableColumn[]> => {
    if (tableCols[t]) return tableCols[t];
    setLoadingCols((m) => ({ ...m, [t]: true }));
    try {
      const r = await api.listColumns(connectionId, schema, t, database || undefined);
      const cols = r.data.columns ?? [];
      setTableCols((m) => ({ ...m, [t]: cols }));
      return cols;
    } catch {
      setTableCols((m) => ({ ...m, [t]: [] }));
      return [];
    } finally {
      setLoadingCols((m) => ({ ...m, [t]: false }));
    }
  };

  useEffect(() => {
    if (editMode) return; // edit focuses on checks; scope/load-config are fixed
    setConfigs((prev) => {
      const next = { ...prev };
      for (const t of tables) if (!next[t]) next[t] = { load_pattern: "unknown" };
      return next;
    });
    tables.forEach((t) => { if (!tableCols[t] && !loadingCols[t]) void fetchCols(t); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tables]);

  const autoDetect = async (t: string) => {
    setDetecting((m) => ({ ...m, [t]: true }));
    try {
      const cols = await fetchCols(t);
      const r = await api.classifyPattern(cols.map((c) => ({ name: c.name, data_type: c.data_type })));
      const d = r.data as Record<string, unknown>;
      setConfigs((prev) => ({
        ...prev,
        [t]: {
          load_pattern: (d.load_pattern as string) ?? "unknown",
          watermark_column: d.watermark_column as string | undefined,
          watermark_type: d.watermark_type as string | undefined,
          changed_at_column: d.changed_at_column as string | undefined,
          effective_from_column: d.effective_from_column as string | undefined,
          current_flag_column: d.current_flag_column as string | undefined,
          partition_column: d.partition_column as string | undefined,
          freshness_sla_seconds: (d.freshness_sla_seconds as number | undefined) ?? null,
        },
      }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setDetecting((m) => ({ ...m, [t]: false }));
    }
  };
  const autoDetectAll = async () => {
    setDetectingAll(true);
    for (const t of tables) await autoDetect(t);
    setDetectingAll(false);
  };

  const toggleSel = (t: string, on: boolean) =>
    setSel((prev) => { const n = new Set(prev); if (on) n.add(t); else n.delete(t); return n; });
  // Stamp the bulk pattern (and only the fields that pattern uses) onto every selected table.
  const applyBulk = () => {
    const spec = specs?.patterns.find((p) => p.pattern === bulkCfg.load_pattern);
    const keep = new Set(spec?.fields ?? []);
    const next: TableLoadConfig = { load_pattern: bulkCfg.load_pattern };
    for (const [k, v] of Object.entries(bulkCfg)) {
      if (k !== "load_pattern" && keep.has(k) && v !== undefined && v !== "") (next as unknown as Record<string, unknown>)[k] = v;
    }
    setConfigs((prev) => {
      const out = { ...prev };
      for (const t of sel) out[t] = { ...next };
      return out;
    });
  };

  const canSelect = Boolean(name && connectionId && tables.length > 0);

  const monitorLoadType = (): string => {
    const counts: Record<string, number> = {};
    for (const t of tables) {
      const lt = patternToLoadType(configs[t]?.load_pattern ?? "unknown");
      counts[lt] = (counts[lt] ?? 0) + 1;
    }
    return Object.entries(counts).sort((a, b) => b[1] - a[1])[0]?.[0] ?? "incremental";
  };

  // Edit mode: save threshold/schedule/name changes to the existing monitor (same wizard).
  const save = async () => {
    if (!monitorId) return;
    setBusy(true); setErr(null);
    try {
      await api.updateMonitor(monitorId, {
        name,
        schedule_cron: checksToCron(checks),
        lookback_days: lookbackDays,
        alert_policy: checksToAlertPolicy(checks) as never,
      });
      if (checks.dq.name.trim() && checks.dq.sql.trim()) {
        const ref = [database, schema, tables[0]].filter(Boolean).join(".");
        await api.createRule(monitorId, {
          name: checks.dq.name.trim(), type: "custom_sql", table_ref: ref,
          severity: "warning", config: { sql: checks.dq.sql.trim(), warn_at: checks.dq.warnAt, error_at: checks.dq.errorAt },
        }).catch(() => undefined);
      }
      nav(`/observability/${monitorId}`);
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };

  const create = async () => {
    setBusy(true); setErr(null);
    try {
      const wmCols = new Set(tables.map((t) => configs[t]?.watermark_column).filter(Boolean));
      const commonWm = wmCols.size === 1 ? [...wmCols][0]! : null;
      const commonWmType = commonWm ? tables.map((t) => configs[t]?.watermark_type).find(Boolean) ?? "timestamp" : null;

      const mon = await api.createMonitor({
        connection_id: connectionId,
        name,
        scope: { database: database || undefined, schema, tables },
        watermark_column: commonWm,
        watermark_type: commonWmType,
        load_type: monitorLoadType(),
        lookback_days: lookbackDays,
        schedule_cron: checksToCron(checks),
        alert_policy: checksToAlertPolicy(checks),
        metadata_columns: metadataPreset && presets[metadataPreset]
          ? { preset: metadataPreset, roles: presets[metadataPreset].roles }
          : undefined,
        metadata_convention_ids: metadataPreset ? [metadataPreset] : undefined,
      });

      for (const t of tables) {
        const cfg = configs[t] ?? { load_pattern: "unknown" };
        const ref = [database, schema, t].filter(Boolean).join(".");
        const body: Record<string, unknown> = { load_pattern: cfg.load_pattern, source: "user" };
        for (const k of ["watermark_column", "watermark_type", "changed_at_column", "effective_from_column",
          "effective_to_column", "current_flag_column", "partition_column", "batch_id_column",
          "business_key", "active_mode", "high_date_value", "open_end_is_null", "current_flag_value",
          "freshness_sla_seconds"] as const) {
          const v = cfg[k];
          if (v !== undefined && v !== "" && v !== null) body[k] = v;
        }
        // Per-table check override (threshold exceptions) → stored on the table's config.
        if (overrides[t] && Object.keys(overrides[t]).length) body.config = { alert_policy: overrides[t] };
        await api.setTableMetadata(ref, body).catch(() => undefined);
        await api.profileTable(mon.data.id, ref).catch(() => undefined);
        // SCD2 tables get their load-aware rule pack (active-unique, no-overlap, freshness +
        // active-count metric) generated automatically, labelled origin=system.
        if (cfg.load_pattern === "scd2") {
          await api.autogenerateLoadAware(mon.data.id, ref).catch(() => undefined);
        }
      }

      // Auto-create suggested DQ rules for the checked tables, from a sample of the last batch
      // (no full scan). They land as suggested — the user accepts or removes them afterwards.
      if (genRules) {
        const pick = tables.filter((t) => !noGenTables.has(t));
        if (pick.length) {
          await api.suggestPack(mon.data.id, { tables: pick, sample_size: genSampleSize }).catch(() => undefined);
        }
      }

      // Optional custom DQ rule from the Configure-checks step.
      if (checks.dq.name.trim() && checks.dq.sql.trim()) {
        const ref = [database, schema, tables[0]].filter(Boolean).join(".");
        await api.createRule(mon.data.id, {
          name: checks.dq.name.trim(), type: "custom_sql", table_ref: ref,
          severity: "warning", config: { sql: checks.dq.sql.trim(), warn_at: checks.dq.warnAt, error_at: checks.dq.errorAt },
        }).catch(() => undefined);
      }

      // Kick off an initial scan so the dashboard shows the monitor working, then land on the
      // observability list (fire-and-forget — the list reflects status as the scan completes).
      api.runMonitor(mon.data.id).catch(() => undefined);
      nav("/observability");
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };

  if (connections === null || !editLoaded) return <Spinner />;

  const sourceLabel = selected ? `${selected.name} · ${selected.source_type}` : "—";
  const contextCard = (
    <Card className="flex flex-wrap items-center justify-between gap-3 p-4">
      <div className="flex items-center gap-3">
        <span className="grid h-9 w-9 place-items-center rounded-lg bg-teal/10 text-teal-600">
          <Icon name="observability" className="h-5 w-5" />
        </span>
        <div>
          <div className="font-semibold text-navy">{name}</div>
          <div className="text-xs text-slate-400">
            {sourceLabel} · {schema || "—"} · {tables.length} {tables.length === 1 ? "table" : "tables"}
          </div>
        </div>
        {database && <Pill tone="neutral" dot={false}>{database}</Pill>}
      </div>
      <button className="text-sm font-medium text-teal-700 hover:underline" onClick={() => setStep(0)}>
        Manage scope &amp; overrides →
      </button>
    </Card>
  );

  return (
    <div className="mx-auto max-w-[1080px] space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <button onClick={() => nav(-1)} className="text-sm text-slate-400 hover:text-slate-600">← Back</button>
          <h1 className="mt-1 text-2xl font-bold text-navy">{editMode ? "Edit monitor" : "Create monitor"}</h1>
          <p className="text-sm text-slate-500">Set the signals that matter. Start with shared defaults, then fine-tune the exceptions.</p>
        </div>
        <CheckCount checks={16} pillars={5} />
      </div>

      <Card className="p-4"><Stepper steps={STEPS} current={step} /></Card>

      {/* ---------------- Step 1: Select data (edit = read-only scope) ---------------- */}
      {step === 0 && editMode && (
        <div className="space-y-5">
          <Card className="space-y-5 p-6">
            <Field label="Monitor name">
              <input value={name} onChange={(e) => setName(e.target.value)}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal focus:ring-2 focus:ring-teal/20" />
            </Field>
            <div>
              <div className="mb-1 text-sm font-medium text-slate-700">Scope</div>
              <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-sm text-slate-600">
                <div>{sourceLabel}</div>
                <div className="mt-0.5 text-xs text-slate-400">{database || "—"} · {schema || "—"} · {tables.join(", ") || "—"}</div>
                <div className="mt-1 text-xs text-slate-400">Scope and per-table load config can't be changed here — editing adjusts checks, schedule and ownership.</div>
              </div>
            </div>
            <Field label="Analysis window" hint="How far back to analyze for baselines & charts.">
              <select value={lookbackDays} onChange={(e) => setLookbackDays(Number(e.target.value))}
                className="w-56 rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal focus:ring-2 focus:ring-teal/20">
                {[7, 14, 30, 60, 90, 180, 365].map((d) => <option key={d} value={d}>Last {d} days</option>)}
              </select>
            </Field>
          </Card>
        </div>
      )}
      {step === 0 && !editMode && (
        <div className="space-y-5">
          <Card className="space-y-5 p-6">
            <div className="grid gap-4 md:grid-cols-2">
              <Field label="Monitor name">
                <input value={name} onChange={(e) => setName(e.target.value)}
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal focus:ring-2 focus:ring-teal/20" />
              </Field>
              <Field label="Connection">
                <Combobox value={connectionId} onChange={setConnectionId} allowCustom={false}
                  placeholder={connections.length === 0 ? "No connections — add one first" : "Select a connection…"}
                  options={connections.map((c) => ({ value: c.id, label: `${c.name} (${c.source_type})` }))} />
              </Field>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <Field label="Database" hint={loadingDbs ? "Loading…" : dbWarn ?? "Read access only."}>
                <Combobox value={database} onChange={setDatabase} options={databases} loading={loadingDbs} placeholder="select or type a database" />
              </Field>
              <Field label="Layer">
                <Combobox value={layer} onChange={setLayer} allowCustom={false} options={["Source", "Staging", "Mart"]} />
              </Field>
            </div>
            <Field label="Schema" hint={loadingSchemas ? "Loading…" : schemaWarn ?? undefined}>
              <Combobox value={schema} onChange={setSchema} options={schemas} loading={loadingSchemas} placeholder="select or type a schema" />
            </Field>
            <Field label="Tables & views"
              hint={loadingTables ? "Loading…" : tablesWarn ?? "Search the connection's read-only catalog, or type a name."}>
              {loadingTables ? <Spinner label="Loading tables & views…" /> : <TablePicker items={catalogTables} value={tables} onChange={setTables} />}
            </Field>
          </Card>

          {tables.length > 0 && (
            <Card className="space-y-3 p-6">
              <div>
                <h2 className="text-sm font-bold uppercase tracking-wide text-navy">ETL metadata columns</h2>
                <p className="mt-1 text-xs text-slate-400">
                  Most pipelines stamp standard audit columns (load time, batch id, SCD2 dates) into every
                  table. Pick your tool to prefill them across all tables — you can still adjust per table below.
                </p>
              </div>
              <Field label="ETL tool preset" hint="Inherited from the connection when set; overridable here.">
                <Combobox value={metadataPreset} onChange={setMetadataPreset} allowCustom={false}
                  options={[{ value: "", label: "— none / custom —" },
                    ...Object.entries(presets).map(([k, v]) => ({ value: k, label: v.label }))]} />
              </Field>
              {metadataPreset && presets[metadataPreset] && (
                <p className="text-xs text-slate-400">
                  Roles:{" "}
                  {Object.entries(presets[metadataPreset].roles).map(([r, c]) => `${r} → ${c}`).join(" · ") || "—"}
                </p>
              )}
            </Card>
          )}

          {tables.length > 0 && specs && (
            <Card className="space-y-4 p-6">
              <div className="flex items-center justify-between">
                <div>
                  <h2 className="text-sm font-bold uppercase tracking-wide text-navy">Load pattern (per table)</h2>
                  <p className="mt-1 text-xs text-slate-400">How each table is loaded — drives freshness, volume and watermark behavior.</p>
                </div>
                {tables.length <= 15 && (
                  <Button variant="ghost" onClick={autoDetectAll} disabled={detectingAll}>{detectingAll ? "Detecting…" : "Auto-detect all"}</Button>
                )}
              </div>
              {tables.length > 15 ? (
                // Many tables → a scannable, progressive detection table instead of 100 cards.
                <LoadTypeDetectionTable
                  connectionId={connectionId} database={database || undefined} schema={schema}
                  tables={tables} patterns={specs.patterns}
                  onApply={(byTable) =>
                    setConfigs((prev) => {
                      const next = { ...prev };
                      for (const [t, pattern] of Object.entries(byTable)) {
                        next[t] = { ...(next[t] ?? { load_pattern: "unknown" }), load_pattern: pattern };
                      }
                      return next;
                    })
                  }
                />
              ) : (
                <>
                  {tables.length > 1 && (
                    <BulkApplyPanel
                      specs={specs} value={bulkCfg} onChange={setBulkCfg} onApply={applyBulk}
                      count={sel.size} total={tables.length}
                      onSelectAll={() => setSel(new Set(tables))} onSelectNone={() => setSel(new Set())}
                    />
                  )}
                  <div className="space-y-3">
                    {tables.map((t) => (
                      <TableLoadConfigRow key={t} table={t} columns={tableCols[t] ?? []} loadingColumns={!!loadingCols[t]} specs={specs}
                        value={configs[t] ?? { load_pattern: "unknown" }} onChange={(next) => setConfigs((prev) => ({ ...prev, [t]: next }))}
                        onAutoDetect={() => autoDetect(t)} detecting={!!detecting[t]}
                        selected={sel.has(t)} onToggleSelect={(on) => toggleSel(t, on)} />
                    ))}
                  </div>
                </>
              )}
              <Field label="Analysis window" hint="How far back to analyze for baselines & charts.">
                <select value={lookbackDays} onChange={(e) => setLookbackDays(Number(e.target.value))}
                  className="w-56 rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal focus:ring-2 focus:ring-teal/20">
                  {[7, 14, 30, 60, 90, 180, 365].map((d) => <option key={d} value={d}>Last {d} days</option>)}
                </select>
              </Field>
            </Card>
          )}

          {tables.length > 0 && (
            <Card className="space-y-3 p-6">
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={genRules} onChange={(e) => setGenRules(e.target.checked)} />
                <span className="text-sm font-bold uppercase tracking-wide text-navy">Auto-create DQ rules</span>
              </label>
              <p className="text-xs text-slate-400">
                On create, we profile a <strong>sample of each table's last batch</strong> (no full scan) and
                generate suggested rules you can accept or remove. Uncheck a table to skip it.
              </p>
              {genRules && (
                <>
                  <div className="flex flex-wrap gap-2">
                    {tables.map((t) => {
                      const on = !noGenTables.has(t);
                      return (
                        <button key={t} type="button"
                          onClick={() => setNoGenTables((prev) => {
                            const next = new Set(prev); if (on) next.add(t); else next.delete(t); return next;
                          })}
                          className={`rounded-full border px-2.5 py-1 text-[11px] font-medium ${on
                            ? "border-teal/40 bg-teal/10 text-teal-700" : "border-slate-200 bg-white text-slate-400"}`}>
                          {on ? "✓ " : ""}{t}
                        </button>
                      );
                    })}
                  </div>
                  <Field label="Sample size (rows per table)" hint="Rows pulled from the last batch to profile & validate.">
                    <input type="number" min={1} max={100000} value={genSampleSize}
                      onChange={(e) => setGenSampleSize(Math.max(1, Math.min(100000, Number(e.target.value) || 1)))}
                      className="w-40 rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal focus:ring-2 focus:ring-teal/20" />
                  </Field>
                </>
              )}
            </Card>
          )}
        </div>
      )}

      {/* ---------------- Step 2: Configure checks ---------------- */}
      {step === 1 && (
        <div className="space-y-6">
          <ConfigureChecks
            value={checks}
            onChange={setChecks}
            context={contextCard}
            onInferFreshness={
              editMode && monitorId && tables[0]
                ? async () => {
                    const ref = [database, schema, tables[0]].filter(Boolean).join(".");
                    const r = await api.suggestFreshness(monitorId, ref);
                    return (r.data.suggestion as unknown as FreshnessSuggestion) ?? null;
                  }
                : undefined
            }
          />
          {!editMode && tables.length > 0 && (
            <PerTableOverrides tables={tables} value={overrides} onChange={setOverrides} defaults={checks} />
          )}
        </div>
      )}

      {/* ---------------- Step 3: Review ---------------- */}
      {step === 2 && (
        <div className="space-y-5">
          {contextCard}
          <div className="grid gap-4 lg:grid-cols-2">
            <Card className="p-5">
              <div className="mb-3 font-semibold text-navy">Thresholds</div>
              <dl className="space-y-1.5 text-sm">
                <Row k="Freshness" v={`warn ${checks.freshness.warnHours}h · error ${checks.freshness.errorHours}h`} />
                <Row k="Volume" v={`${checks.volume.direction} · warn ${checks.volume.warnPct}% · error ${checks.volume.errorPct}%`} />
                <Row k="Schema" v={`added ${checks.schema.added} · removed ${checks.schema.removed} · type ${checks.schema.typeChanged}`} />
                <Row k="Distribution" v={`${checks.distribution.method} · warn ${checks.distribution.warn} · error ${checks.distribution.error}`} />
                {checks.dq.name && <Row k="Custom SQL" v={checks.dq.name} />}
              </dl>
            </Card>
            <Card className="p-5">
              <div className="mb-3 font-semibold text-navy">Execution</div>
              <dl className="space-y-1.5 text-sm">
                <Row k="Tables" v={tables.join(", ") || "—"} />
                <Row k="Schedule" v={checks.schedule} />
                <Row k="Owner" v={checks.owner} />
                <Row k="Notify" v={[checks.notify.warn && "warn", checks.notify.error && "error", checks.notify.recovery && "recovery"].filter(Boolean).join(" · ") || "off"} />
              </dl>
            </Card>
          </div>
        </div>
      )}

      {err && <p className="text-sm text-rose-600">{err}</p>}

      <div className="flex items-center justify-between">
        <Button variant="ghost" onClick={() => (step === 0 ? nav(-1) : setStep(step - 1))}>
          {step === 0 ? "Cancel" : "← Back"}
        </Button>
        <div className="flex items-center gap-3">
          <span className="text-xs text-slate-400">Step {step + 1} of {STEPS.length}</span>
          {step < 2 ? (
            <Button onClick={() => setStep(step + 1)} disabled={step === 0 && !editMode && !canSelect}>Continue</Button>
          ) : editMode ? (
            <Button onClick={save} disabled={busy}>{busy ? "Saving…" : "Save changes"}</Button>
          ) : (
            <Button onClick={create} disabled={!canSelect || busy}>{busy ? "Creating…" : "Create & profile"}</Button>
          )}
        </div>
      </div>
    </div>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex gap-3">
      <dt className="w-28 shrink-0 text-slate-400">{k}</dt>
      <dd className="min-w-0 flex-1 truncate text-slate-700">{v}</dd>
    </div>
  );
}
