"""MCP server — a thin adapter over the same services the REST API uses
(docs/design/06-mcp.md). Tenant scope comes from the session, never from tool args.

The `mcp` SDK is an optional dependency (extra: `mcp`). This module defines the tool
catalog and handlers in a way that works with or without the SDK installed; in local
desktop mode it is served over stdio so a desktop agent connects directly.

KEEP IN SYNC: every key in ``TOOL_CATALOG`` must have a same-named method on
``McpToolRouter`` (and vice-versa) — ``tests/test_mcp_server.py`` enforces this so the MCP
surface never drifts behind the API as new capabilities land.
"""

from __future__ import annotations

from dvp.abstractions.factory import Container
from dvp.core.tenancy import TenantContext
from dvp.observability.service import MonitorService
from dvp.reporting.service import ReportService
from dvp.services.connections import ConnectionService
from dvp.services.projects import ProjectService
from dvp.services.tags import TagService
from dvp.testing_engine.service import CompareService

# Tool catalog (name → human description). Mirrors the REST surface so agents get the full
# product: identity, connections, projects, tags, monitors, compares, and reports.
TOOL_CATALOG: dict[str, str] = {
    "whoami": "Return the caller's user, active group, and role.",
    # connections
    "list_connections": "List the group's data source connections.",
    # projects & tags
    "list_projects": "List projects (groups of test cases).",
    "get_project": "Get a project.",
    "create_project": "Create a project (requires WRITE).",
    "project_test_cases": "List the test cases (compares + monitors) in a project.",
    "list_tags": "List the group's managed tags with usage counts.",
    "create_tag": "Create a managed tag (requires WRITE).",
    # observability
    "list_monitors": "List the group's monitors.",
    "get_monitor": "Get a monitor with its rules.",
    "create_monitor": "Create a monitor (requires WRITE).",
    # data validation
    "list_compares": "List compare test cases with their latest summary.",
    "create_compare": "Create a compare test case (count|schema|data) — requires WRITE.",
    "submit_compare": "Run a compare asynchronously on the worker tier (requires WRITE).",
    "get_compare_result": "Fetch a compare job's summary + result.",
    "suggest_mapping": "Preview auto-detected keys + column mapping for a source/target pair.",
    # reports
    "list_reports": "List reports.",
    "create_report": "Create a report (data_validation|data_quality|executive|profile).",
    "generate_report": "Generate a new immutable report version (requires WRITE).",
    "get_report": "Get a report with its latest document.",
    "report_versions": "List a report's immutable versions.",
    "report_trend": "Pass-rate trend + latest-vs-previous delta.",
    "deliver_report": "Regenerate + deliver a report digest to its channels (requires WRITE).",
}


class McpToolRouter:
    """Dispatches MCP tool calls to the service layer under a fixed TenantContext.

    The context is bound at session creation from the authenticated identity; tools
    cannot override it (isolation guarantee, docs/design/06 §1)."""

    def __init__(self, container: Container, ctx: TenantContext) -> None:
        self.container = container
        self.ctx = ctx

    # ----- service factories (same wiring as the REST routers) -----
    def _compares(self) -> CompareService:
        c = self.container
        return CompareService(c.metadata, c.results, c.secrets, c.spark)

    def _reports(self) -> ReportService:
        c = self.container
        return ReportService(c.metadata, c.results, c.secrets, c.notifier)

    # ----- identity -----
    def whoami(self) -> dict:
        return {"user_id": self.ctx.user_id, "group_id": self.ctx.group_id, "role": self.ctx.role}

    # ----- connections -----
    def list_connections(self) -> list[dict]:
        return ConnectionService(self.container.metadata, self.container.secrets).list(self.ctx)

    # ----- projects & tags -----
    def list_projects(self) -> list[dict]:
        return ProjectService(self.container.metadata).list(self.ctx)

    def get_project(self, project_id: str) -> dict:
        return ProjectService(self.container.metadata).get(self.ctx, project_id)

    def create_project(
        self, name: str, description: str | None = None, tags: list | None = None
    ) -> dict:
        return ProjectService(self.container.metadata).create(
            self.ctx, name=name, description=description, tags=tags
        )

    def project_test_cases(self, project_id: str) -> dict:
        return ProjectService(self.container.metadata).test_cases(self.ctx, project_id)

    def list_tags(self) -> list[dict]:
        return TagService(self.container.metadata).list(self.ctx)

    def create_tag(
        self, name: str, color: str | None = None, description: str | None = None
    ) -> dict:
        return TagService(self.container.metadata).create(
            self.ctx, name=name, color=color, description=description
        )

    # ----- observability -----
    def list_monitors(self) -> list[dict]:
        return MonitorService(self.container.metadata).list(self.ctx)

    def get_monitor(self, monitor_id: str) -> dict:
        return MonitorService(self.container.metadata).get(self.ctx, monitor_id)

    def create_monitor(self, connection_id: str, name: str, scope: dict | None = None) -> dict:
        return MonitorService(self.container.metadata).create(
            self.ctx, connection_id=connection_id, name=name, scope=scope or {}
        )

    # ----- data validation -----
    def list_compares(self) -> list[dict]:
        return self._compares().list_jobs(self.ctx)

    def create_compare(
        self,
        kind: str,
        source: dict,
        target: dict,
        name: str | None = None,
        options: dict | None = None,
    ) -> dict:
        return self._compares().create_job(
            self.ctx, kind=kind, source=source, target=target, name=name, options=options
        )

    def submit_compare(self, job_id: str, variables: dict | None = None) -> dict:
        svc = self._compares()
        self.container.jobs.submit(svc.run_safe, self.ctx, job_id, variables or {})
        return {"job_id": job_id, "status": "queued"}

    def get_compare_result(self, job_id: str) -> dict:
        return self._compares().get_job(self.ctx, job_id)

    def suggest_mapping(self, source: dict, target: dict) -> dict:
        return self._compares().suggest_mapping(self.ctx, source=source, target=target)

    # ----- reports -----
    def list_reports(self) -> list[dict]:
        return self._reports().list_reports(self.ctx)

    def create_report(
        self,
        name: str,
        report_type: str,
        project_id: str | None = None,
        spec: dict | None = None,
        schedule_cron: str | None = None,
        delivery: dict | None = None,
    ) -> dict:
        return self._reports().create_report(
            self.ctx,
            name=name,
            report_type=report_type,
            project_id=project_id,
            spec=spec,
            schedule_cron=schedule_cron,
            delivery=delivery,
        )

    def generate_report(self, report_id: str) -> dict:
        return self._reports().generate(self.ctx, report_id)

    def get_report(self, report_id: str) -> dict:
        return self._reports().get_report(self.ctx, report_id)

    def report_versions(self, report_id: str) -> list[dict]:
        return self._reports().list_versions(self.ctx, report_id)

    def report_trend(self, report_id: str) -> dict:
        return self._reports().trend(self.ctx, report_id)

    def deliver_report(self, report_id: str) -> dict:
        return self._reports().deliver(self.ctx, report_id, regenerate=True)


def build_stdio_server(container: Container, ctx: TenantContext):  # pragma: no cover
    """Construct an MCP stdio server exposing every tool in TOOL_CATALOG."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "MCP SDK not installed. Install the 'mcp' extra: pip install 'dv-platform[mcp]'"
        ) from exc

    server = FastMCP("dv-platform")
    router = McpToolRouter(container, ctx)
    # Register every catalog tool by binding the router method of the same name.
    for tool_name, description in TOOL_CATALOG.items():
        method = getattr(router, tool_name)
        server.add_tool(method, name=tool_name, description=description)
    return server
