from __future__ import annotations

import pytest

from dvp.abstractions.factory import build_container, build_identity_provider
from dvp.config import Settings
from dvp.core.tenancy import Role, TenantContext
from dvp.db.models import Group, Membership, Organization, User
from dvp.services.tenancy import bootstrap_local_tenant, resolve_context


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        profile="local",
        database_url="sqlite://",  # ephemeral in-memory DB — each test gets a clean store
        result_local_dir=str(tmp_path / "results"),
        secret_backend="memory",
        result_backend="local",
    )


@pytest.fixture
def container(settings):
    c = build_container(settings)
    c.metadata.create_all()
    local = bootstrap_local_tenant(c.metadata, settings)
    c.identity = build_identity_provider(
        settings, local_user_id=local.user_id, local_org_id=local.org_id
    )
    c._local = local  # type: ignore[attr-defined]
    return c


@pytest.fixture
def ctx(container) -> TenantContext:
    local = container._local
    return resolve_context(
        container.metadata, user_id=local.user_id, org_id=local.org_id, group_id=local.group_id
    )


@pytest.fixture
def other_group_ctx(container) -> TenantContext:
    """A second, fully independent group+user — for isolation tests."""
    with container.metadata.session() as s:
        org = Organization(name="Org B")
        s.add(org)
        s.flush()
        group = Group(org_id=org.id, name="Group B")
        s.add(group)
        s.flush()
        user = User(email="userb@example.com", display_name="User B")
        s.add(user)
        s.flush()
        s.add(Membership(group_id=group.id, user_id=user.id, role=Role.EDITOR.value))
        s.flush()
        ids = (org.id, group.id, user.id)
    return resolve_context(container.metadata, user_id=ids[2], org_id=ids[0], group_id=ids[1])


@pytest.fixture
def other_group_admin_ctx(other_group_ctx) -> TenantContext:
    """Group B's *admin* — same real group/org/user rows as other_group_ctx, elevated to
    GROUP_ADMIN so cross-tenant isolation can be probed for admin-only surfaces (service
    accounts, audit, members) without the probe failing on a mere permission check."""
    return TenantContext.build(
        other_group_ctx.org_id, other_group_ctx.group_id, other_group_ctx.user_id, Role.GROUP_ADMIN
    )


@pytest.fixture
def admin_ctx(ctx) -> TenantContext:
    """Group A elevated to GROUP_ADMIN (the local tenant is already group_admin, but build an
    explicit one so admin-surface tests don't depend on that default)."""
    return TenantContext.build(ctx.org_id, ctx.group_id, ctx.user_id, Role.GROUP_ADMIN)


@pytest.fixture
def client(settings):
    from fastapi.testclient import TestClient

    from dvp.api.app import create_app

    app = create_app(settings)
    with TestClient(app) as c:
        yield c
