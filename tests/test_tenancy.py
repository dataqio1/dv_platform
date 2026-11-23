import pytest

from dvp.core.errors import NotFoundError, PermissionDeniedError
from dvp.core.tenancy import Permission, Role, TenantContext
from dvp.observability.service import MonitorService
from dvp.services.connections import ConnectionService


def test_role_permissions():
    viewer = TenantContext.build("o", "g", "u", Role.VIEWER)
    editor = TenantContext.build("o", "g", "u", Role.EDITOR)
    assert viewer.has(Permission.READ)
    assert not viewer.has(Permission.WRITE)
    assert editor.has(Permission.WRITE)
    with pytest.raises(PermissionDeniedError):
        viewer.require(Permission.WRITE)


def test_cross_group_isolation(container, ctx, other_group_ctx):
    """Group A's monitor is invisible to Group B (docs/design/02, TEN-01)."""
    monitors = MonitorService(container.metadata)
    # Group A needs a connection + monitor.
    conn = ConnectionService(container.metadata, container.secrets).create(
        ctx, source_type="snowflake", name="A", coordinates={}, auth_method="key_pair"
    )
    created = monitors.create(ctx, connection_id=conn["id"], name="A-monitor", scope={})

    # Group B sees nothing and cannot fetch A's monitor by id.
    assert monitors.list(other_group_ctx) == []
    with pytest.raises(NotFoundError):
        monitors.get(other_group_ctx, created["id"])

    # Group A still sees its own.
    assert len(monitors.list(ctx)) == 1


def test_secret_scope_enforced(container, ctx, other_group_ctx):
    from dvp.abstractions.secret_store import secret_path

    path_a = secret_path(ctx, "conn-123")
    container.secrets.put(ctx, path_a, {"key": "value"})
    # Group B cannot read a secret under Group A's path.
    with pytest.raises(PermissionDeniedError):
        container.secrets.get(other_group_ctx, path_a)
