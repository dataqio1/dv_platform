import { useState } from "react";
import { api } from "../api";
import { Button } from "./ui";

export function DbtRunForm({ onRecorded }: { onRecorded: () => void }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setErr(null);
    setMsg(null);
    try {
      const body = JSON.parse(text) as Record<string, unknown>;
      const res = await api.ingestDbtRun(body);
      const d = res.data;
      setMsg(
        `${d.opened} opened, ${d.updated} updated, ${d.resolved} cleared. ${d.failed} failed, ${d.passed} passed, ${d.skipped} skipped.`,
      );
      setText("");
      onRecorded();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="text-xs font-medium text-teal-700 hover:underline"
      >
        Record a dbt run
      </button>
    );
  }

  return (
    <div className="mt-3 space-y-2">
      <p className="text-xs text-slate-500">
        Paste run_results.json. A failed test or model error opens one incident. A pass clears that incident.
      </p>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={6}
        placeholder='{"results":[{"unique_id":"test.analytics.not_null_fct_orders_order_id","status":"fail","message":"Got 3 results"}]}'
        className="w-full rounded-lg border border-slate-300 px-3 py-2 font-mono text-xs outline-none focus:border-teal"
      />
      {err && <p className="text-sm text-rose-600">{err}</p>}
      {msg && <p className="text-sm text-slate-600">{msg}</p>}
      <div className="flex gap-2">
        <Button onClick={submit} disabled={busy || !text.trim()}>
          {busy ? "Recording…" : "Record run"}
        </Button>
        <Button variant="ghost" onClick={() => setOpen(false)} disabled={busy}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
