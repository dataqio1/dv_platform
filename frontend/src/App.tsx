import { useEffect } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import Shell from "./components/Shell";
import FeedbackWidget from "./components/FeedbackWidget";
import Preferences from "./pages/Preferences";
import Connections from "./pages/Connections";
import ConfirmWatch from "./pages/ConfirmWatch";
import Monitors from "./pages/Monitors";
import MonitorDetail from "./pages/MonitorDetail";
import CreateMonitor from "./pages/CreateMonitor";
import TableObservability from "./pages/TableObservability";
import ProjectWorkspace, { CompareFromIncident } from "./pages/DataValidation";
import FindColumn from "./pages/FindColumn";
import CompareDetail from "./pages/CompareDetail";
import CompareEdit from "./pages/CompareEdit";
import CompareClone from "./pages/CompareClone";
import CompareBatchDetail from "./pages/CompareBatchDetail";
import CompareBatchEdit from "./pages/CompareBatchEdit";
import Incidents from "./pages/Incidents";
import Projects from "./pages/Projects";
import Reports from "./pages/Reports";
import AdminLayout from "./pages/admin/AdminLayout";
import AdminMembers from "./pages/admin/Members";
import AdminAccess from "./pages/admin/Access";
import AdminOrganization from "./pages/admin/Organization";
import AdminAudit from "./pages/admin/AuditLog";
import AdminSystem from "./pages/admin/SystemInfo";
import AdminIntegrations from "./pages/admin/Integrations";
import AdminCompute from "./pages/admin/Compute";
import AdminEtlTools from "./pages/admin/EtlTools";
import AdminFeedback from "./pages/admin/Feedback";
import { usePrefs } from "./prefs";

// Top-level sections we remember as "where the user last was".
const SECTIONS = ["observability", "validation", "find-column", "reports", "incidents", "connections"];

// Send the user back to the section they last used.
function RootRedirect() {
  const { prefs, loaded } = usePrefs();
  if (!loaded) return null;
  const last =
    typeof prefs.lastSection === "string" && SECTIONS.includes(prefs.lastSection)
      ? prefs.lastSection
      : "observability";
  return <Navigate to={`/${last}`} replace />;
}

// Persist the current top-level section so we can restore it next login.
function SectionTracker() {
  const { setPref } = usePrefs();
  const loc = useLocation();
  useEffect(() => {
    const seg = loc.pathname.split("/")[1];
    if (SECTIONS.includes(seg)) setPref("lastSection", seg);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loc.pathname]);
  return null;
}

export default function App() {
  return (
    <>
      <SectionTracker />
      <Shell>
        <Routes>
          <Route path="/" element={<RootRedirect />} />
          <Route path="/observability" element={<Monitors />} />
          <Route path="/observability/new" element={<CreateMonitor />} />
          <Route path="/observability/:monitorId/edit" element={<CreateMonitor />} />
          <Route path="/observability/:monitorId" element={<MonitorDetail />} />
          <Route path="/observability/:monitorId/tables/:tableRef" element={<TableObservability />} />
          {/* Project-first: the project list, then a project's test cases. */}
          <Route path="/validation" element={<Projects />} />
          <Route path="/validation/projects/:projectId" element={<ProjectWorkspace />} />
          <Route path="/validation/batch/:batchId/edit" element={<CompareBatchEdit />} />
          <Route path="/validation/batch/:batchId" element={<CompareBatchDetail />} />
          <Route path="/validation/from-incident" element={<CompareFromIncident />} />
          <Route path="/validation/:jobId/edit" element={<CompareEdit />} />
          <Route path="/validation/:jobId/clone" element={<CompareClone />} />
          <Route path="/validation/:jobId" element={<CompareDetail />} />
          {/* Find Column — value → table/column locator (Value Locator MVP) */}
          <Route path="/find-column" element={<FindColumn />} />
          {/* Legacy links */}
          <Route path="/projects" element={<Navigate to="/validation" replace />} />
          <Route path="/validation/projects" element={<Navigate to="/validation" replace />} />
          <Route path="/reports" element={<Reports />} />
          <Route path="/reports/:reportId" element={<Reports />} />
          <Route path="/incidents" element={<Incidents />} />
          {/* Connections now live under Administration; keep old links working. */}
          <Route path="/connections" element={<Navigate to="/admin/connections" replace />} />
          <Route path="/connections/:connectionId/watch" element={<ConfirmWatch />} />
          <Route path="/settings" element={<Preferences />} />
          {/* Admin — RBAC-gated shell with a left-rail; each section checks its own permission. */}
          <Route path="/admin" element={<AdminLayout />}>
            <Route index element={<Navigate to="members" replace />} />
            <Route path="members" element={<AdminMembers />} />
            <Route path="access" element={<AdminAccess />} />
            <Route path="connections" element={<Connections />} />
            <Route path="etl-tools" element={<AdminEtlTools />} />
            <Route path="compute" element={<AdminCompute />} />
            <Route path="integrations" element={<AdminIntegrations />} />
            <Route path="organization" element={<AdminOrganization />} />
            <Route path="feedback" element={<AdminFeedback />} />
            <Route path="audit" element={<AdminAudit />} />
            <Route path="system" element={<AdminSystem />} />
          </Route>
          <Route path="*" element={<Navigate to="/observability" replace />} />
        </Routes>
        <FeedbackWidget />
      </Shell>
    </>
  );
}
