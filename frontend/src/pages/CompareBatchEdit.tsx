import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import type { CompareBatch, Connection } from "../types";
import { Spinner } from "../components/ui";
import { TestCaseWizard } from "../components/TestCaseWizard";

/** Edit a multi-table (batch) test case using the same wizard as creation, pre-filled from its
 *  pairs. Saving updates each pair in place, creates pairs you added, deletes ones you removed,
 *  and applies name/schedule across the batch (no new run). Results stay on the batch page. */
export default function CompareBatchEdit() {
  const { batchId = "" } = useParams();
  const nav = useNavigate();
  const [batch, setBatch] = useState<CompareBatch | null>(null);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.getBatch(batchId).then((r) => setBatch(r.data)).catch((e) => setErr(e.message));
    api.listConnections().then((r) => setConnections(r.data)).catch(() => setConnections([]));
  }, [batchId]);

  if (err) return <p className="text-sm text-rose-600">{err}</p>;
  if (!batch) return <Spinner />;

  const backTo = batch.project_id ? `/validation/projects/${batch.project_id}` : "/validation";
  return (
    <div className="mx-auto max-w-7xl space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2 text-sm text-slate-400">
        <div className="flex items-center gap-2">
          <Link to="/validation" className="hover:text-slate-600">Data Validation</Link>
          <span>/</span>
          <button onClick={() => nav(backTo)} className="hover:text-slate-600">Project</button>
          <span>/</span>
          <span className="font-medium text-navy">Edit: {batch.name}</span>
        </div>
        <Link to={`/validation/batch/${batch.batch_id}`} className="font-medium text-teal-700 hover:underline">
          View results →
        </Link>
      </div>
      <TestCaseWizard
        connections={connections}
        projectId={batch.project_id ?? undefined}
        editBatch={batch}
        onCancel={() => nav(backTo)}
        onNavigate={(p) => nav(p)}
      />
    </div>
  );
}
