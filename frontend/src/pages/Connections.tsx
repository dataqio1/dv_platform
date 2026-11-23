import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import type { Connection, MetadataConvention } from "../types";
import { Button, Card, EmptyState, Field, HealthBadge, Spinner, TextInput } from "../components/ui";
import { Combobox } from "../components/Combobox";
import { SortTh, useSort } from "../components/sortable";

const SAMPLE_COORDS = {
  database: "RAW_SALES",
  schema: "PUBLIC",
  tables: {
    RAW_CUSTOMERS: {
      columns: [
        { name: "ID", data_type: "NUMBER", nullable: false },
        { name: "INGESTED_AT", data_type: "TIMESTAMP_NTZ", nullable: false },
        { name: "EMAIL", data_type: "VARCHAR", nullable: true },
      ],
      row_count: 5000,
      watermarks: { INGESTED_AT: "2026-09-18T10:45:00" },
    },
  },
};

type SourceType = "oracle" | "snowflake" | "file" | "s3" | "local" | "inline";

const SOURCE_LABEL: Record<SourceType, string> = {
  oracle: "Oracle",
  snowflake: "Snowflake",
  file: "File (CSV / JSON / Parquet)",
  s3: "S3 file (CSV / JSON / Parquet)",
  local: "Local SQL (demo, with rows)",
  inline: "Inline (demo, schema only)",
};

export default function Connections() {
  const nav = useNavigate();
  const [rows, setRows] = useState<Connection[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, { ok: boolean; detail: string }>>({});

  // form state
  const [sourceType, setSourceType] = useState<SourceType>("oracle");
  const [name, setName] = useState("oracle-prod");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("1521");
  const [serviceName, setServiceName] = useState("");
  const [account, setAccount] = useState("");
  const [warehouse, setWarehouse] = useState("");
  const [database, setDatabase] = useState("");
  const [schema, setSchema] = useState("");
  const [role, setRole] = useState("");
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  // snowflake auth (SSO browser or key-pair only — no password). SSO is the default:
  // desktop users typically can't provision a key pair, so browser SSO is the norm.
  const [sfAuth, setSfAuth] = useState<"key_pair" | "external_browser">("external_browser");
  const [privateKey, setPrivateKey] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [coordsJson, setCoordsJson] = useState("{\n  \"database\": \"DB\",\n  \"schema\": \"PUBLIC\",\n  \"tables\": {}\n}");
  // file / s3
  const [uri, setUri] = useState("");
  const [fileFormat, setFileFormat] = useState("");
  const [tableName, setTableName] = useState("");
  // scan a prefix/directory → every table one level below becomes a dataset
  const [scan, setScan] = useState(false);
  const [awsKey, setAwsKey] = useState("");
  const [awsSecret, setAwsSecret] = useState("");
  const [awsRegion, setAwsRegion] = useState("");

  const [canManage, setCanManage] = useState(true);
  // ETL metadata-column conventions to attach (shared across connections; attach >1 for a
  // schema loaded by multiple ETL tools). docs/design/11 §3.4.
  const [conventions, setConventions] = useState<MetadataConvention[]>([]);
  const [convIds, setConvIds] = useState<string[]>([]);

  const load = () =>
    api.listConnections().then((r) => setRows(r.data)).catch((e) => setErr(e.message));
  useEffect(() => {
    load();
    // only admins configure connections; everyone else sees them read-only
    api.me().then((r) => setCanManage(["group_admin", "org_admin"].includes(r.data.role))).catch(() => setCanManage(true));
    api.listMetadataConventions().then((r) => setConventions(r.data)).catch(() => undefined);
  }, []);

  const addSample = async () => {
    setBusy(true);
    setErr(null);
    try {
      await api.createConnection({
        source_type: "inline",
        name: "raw-sales-sample",
        auth_method: "none",
        coordinates: SAMPLE_COORDS,
      });
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    setBusy(true);
    setErr(null);
    try {
      let coordinates: Record<string, unknown> = {};
      let auth_method = "none";
      let secret: Record<string, unknown> | undefined;
      if (sourceType === "oracle") {
        coordinates = { host, port: Number(port), service_name: serviceName, schema };
        auth_method = "password";
        secret = { user, password };
      } else if (sourceType === "snowflake") {
        coordinates = { account, warehouse, database, schema, role, user };
        auth_method = sfAuth;
        // key-pair needs the private key; SSO (external_browser) needs no secret.
        secret = sfAuth === "key_pair" ? { private_key: privateKey, passphrase: passphrase || undefined } : undefined;
      } else if (sourceType === "file" || sourceType === "s3") {
        coordinates = scan
          ? { uri, format: fileFormat || undefined, scan: true }
          : { uri, format: fileFormat || undefined, table: tableName || undefined };
        if (sourceType === "s3" && awsKey) {
          auth_method = "aws";
          secret = { aws_access_key_id: awsKey, aws_secret_access_key: awsSecret, region: awsRegion };
        }
      } else {
        coordinates = JSON.parse(coordsJson);
      }
      await api.createConnection({
        source_type: sourceType, name, coordinates, auth_method, secret,
        metadata_convention_ids: convIds.length ? convIds : undefined,
      });
      setAdding(false);
      load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const test = async (id: string) => {
    setTestResult((p) => ({ ...p, [id]: { ok: false, detail: "testing…" } }));
    try {
      const r = await api.testConnection(id);
      setTestResult((p) => ({ ...p, [id]: { ok: r.data.ok, detail: r.data.detail } }));
      if (r.data.ok) nav(`/connections/${id}/watch`);
    } catch (e) {
      setTestResult((p) => ({ ...p, [id]: { ok: false, detail: (e as Error).message } }));
    }
  };

  const del = async (id: string) => {
    await api.deleteConnection(id).catch(() => undefined);
    load();
  };

  const credFields = sourceType === "oracle"; // Oracle uses user/password; Snowflake uses key-pair/SSO
  const fileFields = sourceType === "file" || sourceType === "s3";

  const [search, setSearch] = useState("");
  const filtered = (rows ?? []).filter((c) => {
    const s = search.trim().toLowerCase();
    return !s || [c.name, c.source_type].some((v) => (v || "").toLowerCase().includes(s));
  });
  const { sorted: sortedRows, sort, requestSort } = useSort(filtered, {
    name: (c) => c.name,
    type: (c) => c.source_type,
    secret: (c) => (c.has_secret ? 1 : 0),
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-navy">Connections</h1>
          <p className="text-sm text-slate-500">
            Data sources — Oracle, Snowflake, and local demo sources.
            {!canManage && <span className="ml-2 text-xs text-slate-400">· read-only (admins configure connections)</span>}
          </p>
        </div>
        {canManage && (
          <div className="flex gap-2">
            <Button variant="ghost" onClick={addSample} disabled={busy}>
              + Sample source
            </Button>
            <Button onClick={() => setAdding(!adding)}>{adding ? "Close" : "+ New connection"}</Button>
          </div>
        )}
      </div>

      {adding && (
        <Card className="space-y-4 p-6">
          <div className="grid grid-cols-2 gap-4">
            <Field label="Type">
              <Combobox
                value={sourceType}
                onChange={(v) => setSourceType(v as SourceType)}
                allowCustom={false}
                options={Object.entries(SOURCE_LABEL).map(([k, v]) => ({ value: k, label: v }))}
              />
            </Field>
            <Field label="Name">
              <TextInput value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
          </div>

          {sourceType === "oracle" && (
            <div className="grid grid-cols-2 gap-4">
              <Field label="Host">
                <TextInput value={host} onChange={(e) => setHost(e.target.value)} placeholder="db.example.com" />
              </Field>
              <Field label="Port">
                <TextInput value={port} onChange={(e) => setPort(e.target.value)} />
              </Field>
              <Field label="Service name">
                <TextInput value={serviceName} onChange={(e) => setServiceName(e.target.value)} placeholder="ORCLPDB1" />
              </Field>
              <Field label="Schema (owner)">
                <TextInput value={schema} onChange={(e) => setSchema(e.target.value)} placeholder="HR" />
              </Field>
            </div>
          )}

          {sourceType === "snowflake" && (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <Field label="Account">
                  <TextInput value={account} onChange={(e) => setAccount(e.target.value)} placeholder="xy12345.us-east-1" />
                </Field>
                <Field label="User">
                  <TextInput value={user} onChange={(e) => setUser(e.target.value)} />
                </Field>
                <Field label="Warehouse">
                  <TextInput value={warehouse} onChange={(e) => setWarehouse(e.target.value)} />
                </Field>
                <Field label="Database">
                  <TextInput value={database} onChange={(e) => setDatabase(e.target.value)} />
                </Field>
                <Field label="Schema">
                  <TextInput value={schema} onChange={(e) => setSchema(e.target.value)} />
                </Field>
                <Field label="Role">
                  <TextInput value={role} onChange={(e) => setRole(e.target.value)} />
                </Field>
              </div>
              <Field label="Authentication" hint="Snowflake supports key-pair and SSO (browser) only — no passwords.">
                <select
                  value={sfAuth}
                  onChange={(e) => setSfAuth(e.target.value as typeof sfAuth)}
                  className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal"
                >
                  <option value="external_browser">SSO — browser (desktop; prompted once, token cached)</option>
                  <option value="key_pair">Key-pair (service account / scheduled)</option>
                </select>
              </Field>
              {sfAuth === "key_pair" ? (
                <div className="space-y-3">
                  <Field label="Private key (PEM)" hint="PKCS#8; pasted key is stored in the secret store, never returned">
                    <textarea
                      value={privateKey}
                      onChange={(e) => setPrivateKey(e.target.value)}
                      rows={4}
                      placeholder="-----BEGIN PRIVATE KEY-----"
                      className="w-full rounded-lg border border-slate-300 px-3 py-2 font-mono text-xs outline-none focus:border-teal"
                    />
                  </Field>
                  <Field label="Key passphrase (optional)">
                    <TextInput type="password" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} />
                  </Field>
                </div>
              ) : (
                <p className="rounded-lg bg-slate-50 p-3 text-xs text-slate-500">
                  SSO uses your browser once; Snowflake's driver caches the token so later runs don't
                  re-prompt. No password or key is stored.
                </p>
              )}
            </div>
          )}

          {credFields && (
            <div className="grid grid-cols-2 gap-4">
              <Field label="User">
                <TextInput value={user} onChange={(e) => setUser(e.target.value)} />
              </Field>
              <Field label="Password">
                <TextInput type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
              </Field>
            </div>
          )}

          {fileFields && (
            <>
              <Field
                label={sourceType === "s3" ? "S3 URI" : "File path / URI"}
                hint={scan
                  ? "a prefix / directory — e.g. s3://bucket/lake/raw/ or /data/lake/"
                  : "e.g. s3://bucket/data/orders.parquet or /data/orders.csv"}
              >
                <TextInput value={uri} onChange={(e) => setUri(e.target.value)}
                  placeholder={scan ? "s3://bucket/lake/raw/" : "s3://bucket/orders.csv"} />
              </Field>
              <label className="flex items-start gap-2 rounded-lg border border-slate-200 bg-slate-50 p-3 text-sm">
                <input type="checkbox" className="mt-0.5" checked={scan} onChange={(e) => setScan(e.target.checked)} />
                <span>
                  <span className="font-medium text-navy">Scan this prefix as a schema</span>
                  <span className="block text-xs text-slate-500">
                    Every file or sub-folder one level below becomes its own table (dataset).
                    A sub-folder of part-files loads as a single table.
                  </span>
                </span>
              </label>
              <div className={`grid gap-4 ${scan ? "grid-cols-1" : "grid-cols-2"}`}>
                <Field label="Format" hint="auto-detected from extension if blank">
                  <Combobox
                    value={fileFormat}
                    onChange={setFileFormat}
                    allowCustom={false}
                    placeholder="auto"
                    options={[
                      { value: "", label: "auto" },
                      { value: "csv", label: "csv" },
                      { value: "json", label: "json" },
                      { value: "ndjson", label: "ndjson" },
                      { value: "parquet", label: "parquet" },
                    ]}
                  />
                </Field>
                {!scan && (
                  <Field label="Table name" hint="logical name (default: file name)">
                    <TextInput value={tableName} onChange={(e) => setTableName(e.target.value)} placeholder="ORDERS" />
                  </Field>
                )}
              </div>
              {sourceType === "s3" && (
                <div className="grid grid-cols-3 gap-4">
                  <Field label="AWS key id" hint="blank = default chain">
                    <TextInput value={awsKey} onChange={(e) => setAwsKey(e.target.value)} />
                  </Field>
                  <Field label="AWS secret">
                    <TextInput type="password" value={awsSecret} onChange={(e) => setAwsSecret(e.target.value)} />
                  </Field>
                  <Field label="Region">
                    <TextInput value={awsRegion} onChange={(e) => setAwsRegion(e.target.value)} placeholder="us-east-1" />
                  </Field>
                </div>
              )}
            </>
          )}

          {(sourceType === "local" || sourceType === "inline") && (
            <Field label="Coordinates (JSON)" hint="tables/columns (+rows for Local SQL)">
              <textarea
                value={coordsJson}
                onChange={(e) => setCoordsJson(e.target.value)}
                rows={6}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 font-mono text-xs outline-none focus:border-teal"
              />
            </Field>
          )}

          {conventions.length > 0 && (
            <Field label="ETL metadata conventions" hint="How this source's ETL tool(s) stamp audit columns. Attach one; attach several if the schema is loaded by more than one tool — each table resolves to the best-matching convention automatically.">
              <div className="flex flex-wrap gap-2">
                {conventions.map((c) => {
                  const on = convIds.includes(c.id);
                  return (
                    <button
                      key={c.id}
                      type="button"
                      onClick={() => setConvIds((prev) => (on ? prev.filter((i) => i !== c.id) : [...prev, c.id]))}
                      title={Object.entries(c.roles).map(([r, col]) => `${r} → ${col}`).join(", ") || "no roles"}
                      className={`rounded-full border px-2.5 py-1 text-[11px] font-medium ${on
                        ? "border-teal/50 bg-teal/10 text-teal-700" : "border-slate-200 bg-white text-slate-500 hover:border-slate-300"}`}
                    >
                      {on ? "✓ " : ""}{c.name}{c.system ? " ·sys" : ""}
                    </button>
                  );
                })}
              </div>
            </Field>
          )}

          <p className="text-xs text-slate-400">
            Credentials are written to the secret store and never returned by the API.
          </p>
          {err && <p className="text-xs text-rose-600">{err}</p>}
          <div className="flex justify-end">
            <Button onClick={submit} disabled={!name || busy}>
              {busy ? "Saving…" : "Create connection"}
            </Button>
          </div>
        </Card>
      )}

      {err && !adding && <p className="text-sm text-rose-600">{err}</p>}
      {rows !== null && rows.length > 0 && (
        <div className="relative max-w-sm">
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search connections by name…"
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
          />
        </div>
      )}

      {rows === null ? (
        <Spinner />
      ) : rows.length === 0 ? (
        <EmptyState title="No connections yet" hint="Add an Oracle/Snowflake connection or a sample source." />
      ) : sortedRows.length === 0 ? (
        <EmptyState title="No connections match" hint={`Nothing named like “${search}”.`} />
      ) : (
        <Card>
          <table className="w-full text-sm">
            <thead className="border-b border-slate-200 text-left text-xs uppercase text-slate-400">
              <tr>
                <SortTh label="Name" columnKey="name" sort={sort} onSort={requestSort} className="px-5 py-3 font-medium" />
                <SortTh label="Type" columnKey="type" sort={sort} onSort={requestSort} className="px-5 py-3 font-medium" />
                <SortTh label="Secret" columnKey="secret" sort={sort} onSort={requestSort} className="px-5 py-3 font-medium" />
                <SortTh label="Test" sortable={false} sort={sort} onSort={requestSort} className="px-5 py-3 font-medium" />
                <th className="px-5 py-3 font-medium"></th>
              </tr>
            </thead>
            <tbody>
              {sortedRows.map((c) => (
                <tr key={c.id} className="border-b border-slate-100 last:border-0">
                  <td className="px-5 py-3 font-medium text-navy">{c.name}</td>
                  <td className="px-5 py-3">{c.source_type}</td>
                  <td className="px-5 py-3 text-xs text-slate-400">{c.has_secret ? "yes" : "—"}</td>
                  <td className="px-5 py-3">
                    <div className="flex items-center gap-2">
                      <Button variant="ghost" className="!px-2 !py-1 text-xs" onClick={() => test(c.id)}>
                        Test
                      </Button>
                      {testResult[c.id] && (
                        <span className="flex items-center gap-1 text-xs">
                          <HealthBadge status={testResult[c.id].ok ? "healthy" : "critical"} label={testResult[c.id].ok ? "ok" : "fail"} />
                          <span className="max-w-[260px] truncate text-slate-400">{testResult[c.id].detail}</span>
                        </span>
                      )}
                    </div>
                  </td>
                  <td className="px-5 py-3 text-right">
                    {canManage && (
                      <button onClick={() => del(c.id)} className="text-xs text-slate-300 hover:text-rose-500">
                        delete
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
