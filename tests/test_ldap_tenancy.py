"""LDAP-group-as-tenant: provisioning, role mapping, revocation, isolation (no mocks).

The LDAP *bind* is behind a directory port; these tests use a real in-memory `StaticDirectory`
and the real provisioning/mapping/service code. A live-server bind is covered by the
env-gated functional script tests/scripts/test_ldap_login.sh.
"""

from __future__ import annotations

import pytest

from dvp.abstractions.identity import Identity
from dvp.abstractions.ldap import DirectoryUser, LdapIdentityProvider, StaticDirectory
from dvp.core.errors import AuthError
from dvp.core.tenancy import Role
from dvp.services.tenancy import GroupRoleMapper, TenantProvisioner


def _ident(email, groups):
    return Identity(user_id="", email=email, org_id="", groups=tuple(groups), display_name=email)


# ----------------------------- role mapper -----------------------------
def test_role_mapper_roles_from_role_groups():
    m = GroupRoleMapper(
        admin_groups=["dq-admins"],
        editor_groups=["dq-editors"],
        report_viewer_groups=["dq-reports"],
        default_role=Role.VIEWER,
    )
    assert m.role_for(["dq-admins"]) == Role.GROUP_ADMIN
    assert m.role_for(["dq-reports"]) == Role.REPORT_VIEWER
    assert m.role_for(["analysts"]) == Role.VIEWER  # none matched → default
    # strongest role wins when the user is in several role groups
    assert m.role_for(["dq-reports", "dq-admins"]) == Role.GROUP_ADMIN
    # role groups are never tenants; other groups are
    assert not m.is_tenant("dq-admins") and not m.is_tenant("dq-reports")
    assert m.is_tenant("finance")


def test_role_mapper_tenant_allowlist():
    m = GroupRoleMapper(tenant_groups=["finance", "ops"])
    assert m.is_tenant("finance") and not m.is_tenant("random-group")


# ----------------------------- provisioning -----------------------------
def test_provision_role_group_confers_role_not_tenant(container):
    # `finance` is the tenant; `finance-admins` is a role group (admin), not a second tenant
    mapper = GroupRoleMapper(admin_groups=["finance-admins"], default_role=Role.EDITOR)
    prov = TenantProvisioner(container.metadata, mapper)

    groups = prov.sync(_ident("alice@corp.io", ["finance", "finance-admins"]))
    assert [g["name"] for g in groups] == ["finance"]  # only the tenant group
    assert groups[0]["role"] == "group_admin"  # role conferred by the role group


def test_provision_is_idempotent(container):
    prov = TenantProvisioner(container.metadata, GroupRoleMapper())
    g1 = prov.sync(_ident("bob@corp.io", ["eng"]))
    g2 = prov.sync(_ident("bob@corp.io", ["eng"]))
    assert g1[0]["group_id"] == g2[0]["group_id"]  # same tenant reused, not duplicated


def test_role_change_on_reprovision(container):
    # carol starts as viewer in the `data` tenant, then is added to the admin role group
    prov = TenantProvisioner(
        container.metadata, GroupRoleMapper(admin_groups=["data-admins"], default_role=Role.VIEWER)
    )
    first = prov.sync(_ident("carol@corp.io", ["data"]))
    assert first[0]["role"] == "viewer"
    second = prov.sync(_ident("carol@corp.io", ["data", "data-admins"]))
    assert second[0]["name"] == "data" and second[0]["role"] == "group_admin"  # promoted


def test_revocation_when_group_removed(container):
    prov = TenantProvisioner(container.metadata, GroupRoleMapper())
    both = prov.sync(_ident("dan@corp.io", ["alpha", "beta"]))
    assert len(both) == 2
    # user removed from beta in the directory → only alpha remains
    after = prov.sync(_ident("dan@corp.io", ["alpha"]))
    assert [g["name"] for g in after] == ["alpha"]


# ----------------------------- identity provider -----------------------------
def test_ldap_identity_provider_returns_groups():
    directory = StaticDirectory(
        users={
            "tok-alice": DirectoryUser("alice", "alice@corp.io", "Alice", ("finance", "ops")),
        }
    )
    idp = LdapIdentityProvider(directory)
    ident = idp.authenticate("Bearer tok-alice")
    assert ident.email == "alice@corp.io"
    assert ident.groups == ("finance", "ops")


def test_ldap_identity_rejects_unknown_token():
    idp = LdapIdentityProvider(StaticDirectory(users={}))
    with pytest.raises(AuthError):
        idp.authenticate("Bearer nope")


def test_ldap_identity_rejects_user_without_groups():
    directory = StaticDirectory(users={"t": DirectoryUser("u", "u@corp.io", "U", ())})
    with pytest.raises(AuthError):
        LdapIdentityProvider(directory).authenticate("t")


# ----------------------------- end-to-end isolation via the API -----------------------------
def _ldap_client(settings):
    """A TestClient whose app authenticates via a StaticDirectory (two tenants)."""
    from fastapi.testclient import TestClient

    from dvp.api.app import create_app

    settings.identity_provider = "ldap"
    app = create_app(settings)
    directory = StaticDirectory(
        users={
            "alice": DirectoryUser("alice", "alice@corp.io", "Alice", ("finance",)),
            "bob": DirectoryUser("bob", "bob@corp.io", "Bob", ("ops",)),
        }
    )
    with TestClient(app) as c:
        c.app.state.container.identity = LdapIdentityProvider(directory)
        yield c


@pytest.fixture
def ldap_client(settings):
    yield from _ldap_client(settings)


def test_api_tenant_isolation_across_ldap_groups(ldap_client):
    c = ldap_client
    # alice (finance) and bob (ops) each resolve to their own tenant
    alice = c.get("/api/v1/me", headers={"Authorization": "alice"}).json()["data"]
    bob = c.get("/api/v1/me", headers={"Authorization": "bob"}).json()["data"]
    assert alice["group_id"] != bob["group_id"]
    assert alice["role"] == "editor"

    # alice (editor) builds a test case in her tenant (lands in her group's default project)
    job = c.post(
        "/api/v1/compares",
        headers={"Authorization": "alice"},
        json={"kind": "data", "source": {"table": "A"}, "target": {"table": "B"}},
    ).json()["data"]
    # bob (different LDAP group) cannot see it
    bob_jobs = c.get("/api/v1/compares", headers={"Authorization": "bob"}).json()["data"]
    assert all(j["id"] != job["id"] for j in bob_jobs)
    # alice can
    alice_jobs = c.get("/api/v1/compares", headers={"Authorization": "alice"}).json()["data"]
    assert any(j["id"] == job["id"] for j in alice_jobs)


def test_me_groups_lists_accessible_tenants(ldap_client):
    groups = ldap_client.get("/api/v1/me/groups", headers={"Authorization": "alice"}).json()["data"]
    assert [g["name"] for g in groups] == ["finance"]


def _rbac_client(settings):
    """One tenant ('acme'); four personas via role groups configured in settings."""
    from fastapi.testclient import TestClient

    from dvp.api.app import create_app

    settings.identity_provider = "ldap"
    settings.ldap_admin_groups = ["acme-admins"]
    settings.ldap_editor_groups = ["acme-editors"]
    settings.ldap_report_viewer_groups = ["acme-reporters"]
    settings.ldap_default_role = "viewer"
    app = create_app(settings)
    directory = StaticDirectory(
        users={
            "admin": DirectoryUser("admin", "admin@acme.io", "Admin", ("acme", "acme-admins")),
            "editor": DirectoryUser("editor", "editor@acme.io", "Ed", ("acme", "acme-editors")),
            "viewer": DirectoryUser("viewer", "viewer@acme.io", "Vi", ("acme",)),
            "reporter": DirectoryUser("rep", "rep@acme.io", "Rep", ("acme", "acme-reporters")),
        }
    )
    with TestClient(app) as c:
        c.app.state.container.identity = LdapIdentityProvider(directory)
        yield c


@pytest.fixture
def rbac_client(settings):
    yield from _rbac_client(settings)


_CONN_BODY = {"source_type": "local", "name": "c", "auth_method": "none", "coordinates": {}}


def test_only_admin_can_configure_connections(rbac_client):
    c = rbac_client
    # admin creates a connection
    r = c.post("/api/v1/connections", headers={"Authorization": "admin"}, json=_CONN_BODY)
    assert r.status_code == 201
    # editor + viewer + reporter cannot create connections
    for who in ("editor", "viewer", "reporter"):
        assert c.post("/api/v1/connections", headers={"Authorization": who}, json=_CONN_BODY).status_code == 403


def test_non_report_roles_see_connections_readonly(rbac_client):
    c = rbac_client
    c.post("/api/v1/connections", headers={"Authorization": "admin"}, json=_CONN_BODY)
    # viewer + editor can read connections
    for who in ("viewer", "editor", "admin"):
        assert c.get("/api/v1/connections", headers={"Authorization": who}).status_code == 200
    # report-only persona cannot even read connections (no READ)
    assert c.get("/api/v1/connections", headers={"Authorization": "reporter"}).status_code == 403


def test_report_viewer_can_only_view_reports(rbac_client):
    c = rbac_client
    rep = c.get("/api/v1/reports", headers={"Authorization": "reporter"})
    assert rep.status_code == 200  # VIEW_REPORTS
    # but cannot create reports (no WRITE) or browse projects/compares (no READ)
    assert c.post("/api/v1/reports", headers={"Authorization": "reporter"},
                  json={"name": "x", "report_type": "executive"}).status_code == 403
    assert c.get("/api/v1/projects", headers={"Authorization": "reporter"}).status_code == 403


def test_editor_can_build_test_cases_but_not_connections(rbac_client):
    c = rbac_client
    # editor can build a test case (it lands in the group's default project) ...
    assert c.post("/api/v1/compares", headers={"Authorization": "editor"},
                  json={"kind": "data", "source": {"table": "A"}, "target": {"table": "B"}}
                  ).status_code == 201
    # ... and view reports, but can NOT create projects (admin-only) or configure connections
    assert c.post("/api/v1/projects", headers={"Authorization": "editor"},
                  json={"name": "p"}).status_code == 403
    assert c.get("/api/v1/reports", headers={"Authorization": "editor"}).status_code == 200
    assert c.post("/api/v1/connections", headers={"Authorization": "editor"}, json=_CONN_BODY).status_code == 403


def test_cannot_act_in_foreign_group(ldap_client):
    c = ldap_client
    bob_group = c.get("/api/v1/me", headers={"Authorization": "bob"}).json()["data"]["group_id"]
    # alice tries to act in bob's tenant via X-Group-Id → denied
    r = c.get(
        "/api/v1/projects",
        headers={"Authorization": "alice", "X-Group-Id": bob_group},
    )
    assert r.status_code in (401, 403)
