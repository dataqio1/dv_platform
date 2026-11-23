import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import type { CompareJob, Connection } from "../types";
import { Spinner } from "../components/ui";
import { TestCaseWizard } from "../components/TestCaseWizard";

/** Duplicate an existing test case: opens the create wizard pre-filled from it (name = "Copy of …")
 *  so you can tweak and run a NEW test case without rebuilding it from scratch. */
export default function CompareClone() {
  const { jobId = "" } = useParams();
  const nav = useNavigate();
  const [job, setJob] = useState<CompareJob | null>(null);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.getCompare(jobId).then((r) => setJob(r.data)).catch((e) => setErr(e.message));
    api.listConnections().then((r) => setConnections(r.data)).catch(() => setConnections([]));
  }, [jobId]);

  if (err) return <p className="text-sm text-rose-600">{err}</p>;
  if (!job) return <Spinner />;

  const backTo = job.project_id ? `/validation/projects/${job.project_id}` : "/validation";
  return (
    <div className="mx-auto max-w-7xl space-y-4">
      <div className="flex flex-wrap items-center gap-2 text-sm text-slate-400">
        <Link to="/validation" className="hover:text-slate-600">Data Validation</Link>
        <span>/</span>
        <button onClick={() => nav(backTo)} className="hover:text-slate-600">Project</button>
        <span>/</span>
        <span className="font-medium text-navy">Duplicate: {job.name || job.id.slice(0, 8)}</span>
      </div>
      <TestCaseWizard
        connections={connections}
        projectId={job.project_id ?? undefined}
        cloneJob={job}
        onCancel={() => nav(backTo)}
        onNavigate={(p) => nav(p)}
      />
    </div>
  );
}
