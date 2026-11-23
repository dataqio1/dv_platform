import { useEffect, useState } from "react";
import { api } from "../../api";
import { Button, Card, Field, Spinner, TextInput } from "../../components/ui";

type SettingsKey = "organization" | "auth" | "governance";
type FieldSpec = { key: string; label: string; input: "text" | "number" | "bool"; hint?: string };

// The backend stores each section as a free-form JSON blob; the UI owns the shape. These are
// the fields the Organization-settings screen (Figma) edits. Unknown keys already stored are
// preserved on save (we merge, not replace).
const SECTIONS: { key: SettingsKey; title: string; blurb: string; fields: FieldSpec[] }[] = [
  {
    key: "organization",
    title: "Organization identity",
    blurb: "How this workspace presents itself.",
    fields: [
      { key: "name", label: "Organization name", input: "text" },
      { key: "timezone", label: "Default timezone", input: "text", hint: "e.g. UTC, America/New_York" },
      { key: "support_email", label: "Support email", input: "text" },
    ],
  },
  {
    key: "auth",
    title: "Authentication & session",
    blurb: "Sign-in and session policy.",
    fields: [
      { key: "sso_required", label: "Require SSO", input: "bool" },
      { key: "mfa_required", label: "Require MFA", input: "bool" },
      { key: "session_ttl_minutes", label: "Session timeout (minutes)", input: "number" },
    ],
  },
  {
    key: "governance",
    title: "Governance policies",
    blurb: "Alert-silence limits and data retention.",
    fields: [
      { key: "max_silence_hours", label: "Max alert-silence (hours)", input: "number" },
      { key: "silence_requires_ticket", label: "Require a ticket to silence", input: "bool" },
      { key: "audit_retention_days", label: "Audit retention (days)", input: "number" },
      { key: "deleted_retention_days", label: "Soft-deleted retention (days)", input: "number" },
    ],
  },
];

function SettingsSection({ spec }: { spec: (typeof SECTIONS)[number] }) {
  const [value, setValue] = useState<Record<string, unknown> | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api
      .orgSettings(spec.key)
      .then((r) => setValue(r.data.value ?? {}))
      .catch((e) => setErr((e as Error).message));
  }, [spec.key]);

  const set = (k: string, v: unknown) => {
    setSaved(false);
    setValue((prev) => ({ ...(prev ?? {}), [k]: v }));
  };

  const save = async () => {
    if (!value) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await api.setOrgSettings(spec.key, value);
      setValue(r.data.value ?? {});
      setSaved(true);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="p-5">
      <div className="mb-3">
        <h3 className="text-sm font-semibold text-navy">{spec.title}</h3>
        <p className="text-xs text-slate-500">{spec.blurb}</p>
      </div>
      {value === null ? (
        <Spinner />
      ) : (
        <div className="space-y-3">
          {spec.fields.map((f) => {
            const current = value[f.key];
            if (f.input === "bool") {
              return (
                <label key={f.key} className="flex items-center gap-2 text-sm text-slate-700">
                  <input
                    type="checkbox"
                    checked={Boolean(current)}
                    onChange={(e) => set(f.key, e.target.checked)}
                    className="h-4 w-4 rounded border-slate-300 text-teal focus:ring-teal/30"
                  />
                  {f.label}
                </label>
              );
            }
            return (
              <Field key={f.key} label={f.label} hint={f.hint}>
                <TextInput
                  type={f.input === "number" ? "number" : "text"}
                  value={current === undefined || current === null ? "" : String(current)}
                  onChange={(e) =>
                    set(f.key, f.input === "number" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value)
                  }
                />
              </Field>
            );
          })}
          <div className="flex items-center gap-3 pt-1">
            <Button onClick={save} disabled={busy}>
              Save
            </Button>
            {saved && <span className="text-xs text-emerald-600">Saved.</span>}
            {err && <span className="text-xs text-rose-600">{err}</span>}
          </div>
        </div>
      )}
    </Card>
  );
}

export default function Organization() {
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-bold text-navy">Organization settings</h2>
        <p className="text-sm text-slate-500">Identity, authentication, and governance policies.</p>
      </div>
      {SECTIONS.map((s) => (
        <SettingsSection key={s.key} spec={s} />
      ))}
    </div>
  );
}
