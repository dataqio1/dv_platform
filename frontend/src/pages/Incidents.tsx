import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { Incident } from "../types";
import { Spinner } from "../components/ui";
import { Card, Chip, KpiCard, PageTitle } from "../components/vex";
import { IncidentList, isUnrunCompare } from "../components/IncidentList";
import { SortControl, useSort } from "../components/sortable";

// Worst-first severity rank so sorting by severity surfaces critical alerts.
const SEV_RANK: Record<string, number> = { critical: 0, warning: 1, info: 2, unknown: 3 };

export default function Incidents() {
  const [incidents, setIncidents] = useState<Incident[] | null>(null);
  const [activeOnly, setActiveOnly] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(() => {
    api.listIncidents(activeOnly).then((r) => setIncidents(r.data)).catch((e) => setErr(e.message));
  }, [activeOnly]);
  useEffect(() => { load(); }, [load]);

  const ack = (id: string) => api.ackIncident(id).then(load);
  const resolve = (id: string) => api.resolveIncident(id).then(load);

  const checks = (incidents ?? []).filter(isUnrunCompare);
  const dataIncidents = (incidents ?? []).filter((i) => !isUnrunCompare(i));
  const { sorted: sortedIncidents, sort, requestSort } = useSort(dataIncidents, {
    severity: (i) => SEV_RANK[String(i.severity)] ?? 9,
    newest: (i) => i.created_at,
    occurrences: (i) => i.occurrences,
    table: (i) => i.table_ref,
    status: (i) => i.status,
  });

  const counts = useMemo(() => {
    const c = { critical: 0, warning: 0, acknowledged: 0, silenced: 0 };
    for (const i of dataIncidents) {
      if (i.status === "resolved") continue;
      if (i.status === "acknowledged") c.acknowledged += 1;
      if (i.silenced) c.silenced += 1;
      if (String(i.severity) === "critical") c.critical += 1;
      else if (String(i.severity) === "warning") c.warning += 1;
    }
    return c;
  }, [dataIncidents]);

  return (
    <div className="space-y-6">
      <PageTitle
        title="Incidents"
        subtitle="Deduped, auto-resolving, evidence-backed — across observability and validation."
      />

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <KpiCard label="Critical" value={counts.critical} tone="error" />
        <KpiCard label="Warning" value={counts.warning} tone="attention" />
        <KpiCard label="Acknowledged" value={counts.acknowledged} tone="learning" />
        <KpiCard label="Silenced" value={counts.silenced} tone="paused" />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Chip label="Active only" active={activeOnly} onClick={() => setActiveOnly(true)} />
          <Chip label="All incidents" active={!activeOnly} onClick={() => setActiveOnly(false)} />
        </div>
        {dataIncidents.length > 1 && (
          <SortControl
            options={[
              { key: "severity", label: "Severity" },
              { key: "newest", label: "Newest" },
              { key: "occurrences", label: "Occurrences" },
              { key: "table", label: "Table" },
              { key: "status", label: "Status" },
            ]}
            sort={sort}
            onSort={requestSort}
          />
        )}
      </div>

      {err && <p className="text-sm text-rose-600">{err}</p>}

      <Card className="p-5">
        <h2 className="mb-3 text-sm font-semibold text-navy">Open incidents</h2>
        {incidents === null ? (
          <Spinner />
        ) : (
          <IncidentList
            incidents={sortedIncidents}
            onAck={ack}
            onResolve={resolve}
            onChange={load}
            empty={checks.length > 0 ? "No open incidents." : "No incidents."}
          />
        )}
      </Card>

      {checks.length > 0 && (
        <Card className="p-5">
          <h2 className="text-sm font-semibold text-navy">Could not check</h2>
          <p className="mb-3 mt-0.5 text-xs text-slate-400">The compare did not run. The table is not flagged.</p>
          <IncidentList incidents={checks} onAck={ack} onResolve={resolve} onChange={load} />
        </Card>
      )}
    </div>
  );
}
