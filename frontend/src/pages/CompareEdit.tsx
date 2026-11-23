import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import type { CompareJob, Connection } from "../types";
import { Spinner } from "../components/ui";
import { TestCaseWizard } from "../components/TestCaseWizard";

/** Edit an existing single test case using the same multi-step wizard as creation, pre-filled
 *  from the stored definition. Saving updates the test case in place (no new run). Run results
 *  stay reachable via the "View results" link and the project's Executions tab. */
export default function CompareEdit() {
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
      <div className="flex flex-wrap items-center justify-between gap-2 text-sm text-slate-400">
        <div className="flex items-center gap-2">
          <Link to="/validation" className="hover:text-slate-600">Data Validation</Link>
          <span>/</span>
          <button onClick={() => nav(backTo)} className="hover:text-slate-600">Project</button>
          <span>/</span>
          <span className="font-medium text-navy">Edit: {job.name || job.id.slice(0, 8)}</span>
        </div>
        <Link to={`/validation/${job.id}`} className="font-medium text-teal-700 hover:underline">
          View results →
        </Link>
      </div>
      <TestCaseWizard
        connections={connections}
        projectId={job.project_id ?? undefined}
        editJob={job}
        onCancel={() => nav(backTo)}
        onNavigate={(p) => nav(p)}
      />
    </div>
  );
}
