"""MCP server stays in sync with the service layer (no mocks — real services, real DB).

The guardrail test fails if a tool is added to the catalog without a handler (or vice
versa), so the MCP surface can't silently drift behind the REST API as features land.
"""

from __future__ import annotations

import inspect

from dvp.core.tenancy import Permission
from dvp.mcp.server import TOOL_CATALOG, McpToolRouter


def _router_methods() -> set[str]:
    return {
        name
        for name, _ in inspect.getmembers(McpToolRouter, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


def test_catalog_and_router_in_lockstep():
    assert set(TOOL_CATALOG) == _router_methods(), (
        "MCP catalog and router drifted — every tool needs a catalog entry AND a method"
    )


def test_every_tool_has_a_description():
    assert all(desc.strip() for desc in TOOL_CATALOG.values())


def _router(container, ctx) -> McpToolRouter:
    return McpToolRouter(container, ctx)


def test_whoami_and_reads(container, ctx):
    r = _router(container, ctx)
    who = r.whoami()
    assert who["group_id"] == ctx.group_id
    assert r.list_projects() == []
    assert r.list_tags() == []
    assert r.list_reports() == []
    assert r.list_monitors() == []
    assert r.list_compares() == []
    assert r.list_connections() == []


def test_write_tools_delegate(container, ctx):
    r = _router(container, ctx)
    proj = r.create_project(name="Mig", tags=["q4"])
    assert proj["name"] == "Mig"
    assert r.get_project(proj["id"])["id"] == proj["id"]

    tag = r.create_tag(name="critical", color="#ef4444")
    assert tag["name"] == "critical"
    assert any(t["name"] == "critical" for t in r.list_tags())

    rep = r.create_report(name="R", report_type="data_validation", project_id=proj["id"])
    gen = r.generate_report(rep["id"])
    assert gen["status"] == "ready"
    assert r.report_versions(rep["id"])[0]["version"] == 1


def test_tenant_scope_is_fixed_from_session(container, ctx):
    # the router binds ctx at construction; tools take no tenant args
    r = _router(container, ctx)
    for name in ("list_projects", "list_reports", "whoami"):
        sig = inspect.signature(getattr(r, name))
        assert "ctx" not in sig.parameters and "group_id" not in sig.parameters
    assert ctx.has(Permission.READ)
