"""Append-only audit trail: record + query/filter/export, the READ permission gate, and
cross-tenant isolation (one group can never read another's trail). Real metadata store, no mocks."""

from __future__ import annotations

import csv
import io

import pytest

from dvp.core.errors import PermissionDeniedError, ValidationError
from dvp.core.tenancy import Role, TenantContext
from dvp.services import audit


def _record(container, ctx, action, **kw):
    with container.metadata.session(ctx) as s:
        audit.record(s, ctx, action, **kw)


def _report_viewer(ctx) -> TenantContext:
    return TenantContext.build(ctx.org_id, ctx.group_id, ctx.user_id, Role.REPORT_VIEWER)


# --------------------------------------------------------------------------- basic list


def test_records_listed_newest_first_with_actor(container, ctx):
    _record(container, ctx, "connection.create", entity_type="connection", entity_id="c1")
    _record(container, ctx, "monitor.create", entity_type="monitor", entity_id="m1")

    entries = audit.list_entries(container.metadata, ctx)
    assert [e["action"] for e in entries] == ["monitor.create", "connection.create"]
    # Actor is resolved from the acting user.
    assert entries[0]["user_id"] == ctx.user_id
    assert entries[0]["actor"]  # display name or email, not blank


# --------------------------------------------------------------------------- filters


def test_filters_by_entity_action_and_user(container, ctx):
    _record(container, ctx, "connection.create", entity_type="connection", entity_id="c1")
    _record(container, ctx, "connection.delete", entity_type="connection", entity_id="c1")
    _record(container, ctx, "monitor.create", entity_type="monitor", entity_id="m1")

    by_type = audit.list_entries(container.metadata, ctx, entity_type="connection")
    assert {e["action"] for e in by_type} == {"connection.create", "connection.delete"}

    by_entity = audit.list_entries(
        container.metadata, ctx, entity_type="connection", entity_id="c1"
    )
    assert len(by_entity) == 2

    by_action = audit.list_entries(container.metadata, ctx, action="monitor.create")
    assert [e["entity_id"] for e in by_action] == ["m1"]

    by_user = audit.list_entries(container.metadata, ctx, user_id=ctx.user_id)
    assert len(by_user) == 3
    assert audit.list_entries(container.metadata, ctx, user_id="not-a-user") == []


def test_time_window_and_invalid_timestamp(container, ctx):
    _record(container, ctx, "x.y", entity_type="t", entity_id="1")
    # A future lower bound excludes everything; a past one includes it.
    assert audit.list_entries(container.metadata, ctx, since="2999-01-01T00:00:00+00:00") == []
    assert len(audit.list_entries(container.metadata, ctx, since="2000-01-01T00:00:00+00:00")) == 1
    with pytest.raises(ValidationError):
        audit.list_entries(container.metadata, ctx, since="not-a-timestamp")


def test_limit_is_clamped(container, ctx):
    for i in range(5):
        _record(container, ctx, "bulk", entity_type="t", entity_id=str(i))
    assert len(audit.list_entries(container.metadata, ctx, limit=2)) == 2
    # Over-large limits are clamped, not rejected.
    assert len(audit.list_entries(container.metadata, ctx, limit=10_000)) == 5


# --------------------------------------------------------------------------- CSV export


def test_export_csv_has_header_and_rows(container, ctx):
    _record(
        container,
        ctx,
        "connection.create",
        entity_type="connection",
        entity_id="c1",
        detail={"name": "prod"},
    )
    text = audit.export_csv(container.metadata, ctx)
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["ts", "action", "entity_type", "entity_id", "user_id", "actor", "detail"]
    data = rows[1]
    assert data[1] == "connection.create" and data[2] == "connection" and data[3] == "c1"
    assert "prod" in data[6]  # detail JSON column


# --------------------------------------------------------------------------- permission gate


def test_report_viewer_cannot_read_audit(container, ctx):
    _record(container, ctx, "x.y", entity_type="t", entity_id="1")
    rv = _report_viewer(ctx)
    with pytest.raises(PermissionDeniedError):
        audit.list_entries(container.metadata, rv)
    with pytest.raises(PermissionDeniedError):
        audit.export_csv(container.metadata, rv)


# --------------------------------------------------------------------------- tenant isolation


def test_audit_trail_isolated_across_tenants(container, ctx, other_group_ctx):
    _record(container, ctx, "secret.action", entity_type="connection", entity_id="shared-id")
    # Group B recorded nothing — sees nothing, even probing the same entity_id.
    assert audit.list_entries(container.metadata, other_group_ctx) == []
    assert audit.list_entries(container.metadata, other_group_ctx, entity_id="shared-id") == []
    assert (
        audit.export_csv(container.metadata, other_group_ctx).strip().count("\n") == 0
    )  # header only
    # Group A still sees its own entry.
    assert len(audit.list_entries(container.metadata, ctx)) == 1
