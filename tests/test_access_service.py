"""AccessService — RBAC matrix + service-account (API key) management. Real metadata store,
no mocks. Covers the key-hash discipline (plaintext once, only a hash at rest), role rules,
the permission gate, and cross-tenant isolation (group B's admin cannot see or touch group A's
service accounts)."""

from __future__ import annotations

import hashlib

import pytest

from dvp.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from dvp.core.tenancy import _ROLE_PERMISSIONS, Permission, Role, TenantContext
from dvp.db.models import ServiceAccount
from dvp.services import audit
from dvp.services.access import AccessService, role_matrix


def _editor(ctx) -> TenantContext:
    return TenantContext.build(ctx.org_id, ctx.group_id, ctx.user_id, Role.EDITOR)


# --------------------------------------------------------------------------- role matrix


def test_role_matrix_matches_enforced_permissions():
    m = role_matrix()
    assert m["permissions"] == [p.value for p in Permission]
    by_role = {r["role"]: r for r in m["roles"]}
    # Every role the server enforces is present, and the rendered matrix matches the real grant.
    for role in Role:
        granted = {p.value for p in _ROLE_PERMISSIONS.get(role, set())}
        row = by_role[role.value]
        assert set(row["permissions"]) == granted
        assert row["matrix"] == {p.value: (p.value in granted) for p in Permission}


# --------------------------------------------------------------------------- key lifecycle


def test_create_returns_plaintext_once_and_stores_only_a_hash(container, admin_ctx):
    svc = AccessService(container.metadata)
    created = svc.create_service_account(admin_ctx, name="ci-bot", role="editor")

    key = created["api_key"]
    assert key.startswith("dvp_") and len(key) > 20
    assert created["key_prefix"] and key.startswith(created["key_prefix"])
    assert created["role"] == "editor" and created["active"] is True

    # At rest: only the sha256 hash and the prefix — never the plaintext.
    with container.metadata.session(admin_ctx) as s:
        row = s.get(ServiceAccount, created["id"])
        assert row.key_hash == hashlib.sha256(key.encode()).hexdigest()
        assert key not in (row.key_hash or "")
        assert row.key_prefix == created["key_prefix"]

    # The plaintext is shown exactly once: it never reappears in list output.
    listed = svc.list_service_accounts(admin_ctx)
    assert [a["id"] for a in listed] == [created["id"]]
    assert "api_key" not in listed[0]


def test_rotate_issues_a_new_key_and_invalidates_the_old(container, admin_ctx):
    svc = AccessService(container.metadata)
    created = svc.create_service_account(admin_ctx, name="rot", role="viewer")
    with container.metadata.session(admin_ctx) as s:
        old_hash = s.get(ServiceAccount, created["id"]).key_hash

    rotated = svc.rotate_key(admin_ctx, created["id"])
    assert rotated["api_key"] != created["api_key"]
    with container.metadata.session(admin_ctx) as s:
        new_hash = s.get(ServiceAccount, created["id"]).key_hash
        assert new_hash != old_hash
        assert new_hash == hashlib.sha256(rotated["api_key"].encode()).hexdigest()


def test_delete_removes_the_account(container, admin_ctx):
    svc = AccessService(container.metadata)
    created = svc.create_service_account(admin_ctx, name="gone", role="viewer")
    svc.delete_service_account(admin_ctx, created["id"])
    assert svc.list_service_accounts(admin_ctx) == []
    with pytest.raises(NotFoundError):
        svc.rotate_key(admin_ctx, created["id"])


# --------------------------------------------------------------------------- validation


def test_duplicate_name_rejected(container, admin_ctx):
    svc = AccessService(container.metadata)
    svc.create_service_account(admin_ctx, name="dup", role="viewer")
    with pytest.raises(ValidationError):
        svc.create_service_account(admin_ctx, name="dup", role="viewer")


def test_blank_name_rejected(container, admin_ctx):
    with pytest.raises(ValidationError):
        AccessService(container.metadata).create_service_account(
            admin_ctx, name="  ", role="viewer"
        )


@pytest.mark.parametrize("role", ["org_admin", "nonsense", ""])
def test_unassignable_or_unknown_role_rejected(container, admin_ctx, role):
    """A service account may never be ORG_ADMIN, and bogus roles are refused."""
    with pytest.raises(ValidationError):
        AccessService(container.metadata).create_service_account(admin_ctx, name="x", role=role)


# --------------------------------------------------------------------------- permission gate


def test_editor_cannot_manage_service_accounts(container, ctx):
    svc = AccessService(container.metadata)
    editor = _editor(ctx)
    with pytest.raises(PermissionDeniedError):
        svc.list_service_accounts(editor)
    with pytest.raises(PermissionDeniedError):
        svc.create_service_account(editor, name="nope", role="viewer")


# --------------------------------------------------------------------------- tenant isolation


def test_service_accounts_isolated_across_tenants(container, admin_ctx, other_group_admin_ctx):
    """Group B's admin cannot see, rotate, or delete Group A's service account."""
    svc = AccessService(container.metadata)
    a = svc.create_service_account(admin_ctx, name="a-bot", role="editor")

    assert svc.list_service_accounts(other_group_admin_ctx) == []
    with pytest.raises(NotFoundError):
        svc.rotate_key(other_group_admin_ctx, a["id"])
    with pytest.raises(NotFoundError):
        svc.delete_service_account(other_group_admin_ctx, a["id"])

    # And A's account is untouched.
    assert [x["id"] for x in svc.list_service_accounts(admin_ctx)] == [a["id"]]


# --------------------------------------------------------------------------- audit trail


def test_lifecycle_is_audited(container, admin_ctx):
    svc = AccessService(container.metadata)
    created = svc.create_service_account(admin_ctx, name="audited", role="viewer")
    svc.rotate_key(admin_ctx, created["id"])
    svc.delete_service_account(admin_ctx, created["id"])

    actions = {
        e["action"]
        for e in audit.list_entries(container.metadata, admin_ctx, entity_type="service_account")
    }
    assert {"service_account.create", "service_account.rotate", "service_account.delete"} <= actions
