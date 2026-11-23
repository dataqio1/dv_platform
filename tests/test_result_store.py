"""LocalFileResultStore: round-trip + tenant-scoped delete (D9 isolation)."""

from __future__ import annotations

import pytest

from dvp.abstractions.result_store import LocalFileResultStore
from dvp.core.errors import PermissionDeniedError


class _Ctx:
    def __init__(self, org_id, group_id):
        self.org_id = org_id
        self.group_id = group_id


def test_write_read_delete_roundtrip(tmp_path):
    store = LocalFileResultStore(str(tmp_path))
    ctx = _Ctx("org1", "grpA")
    ref = store.write_detail(ctx, "k", [{"a": 1}, {"a": 2}])
    assert store.read_detail(ctx, ref) == [{"a": 1}, {"a": 2}]
    store.delete(ctx, ref)
    assert store.read_detail(ctx, ref) == []  # gone
    store.delete(ctx, ref)  # idempotent — no error on missing


def test_delete_rejects_cross_tenant_ref(tmp_path):
    store = LocalFileResultStore(str(tmp_path))
    owner = _Ctx("org1", "grpA")
    ref = store.write_detail(owner, "secret", [{"x": 1}])
    attacker = _Ctx("org1", "grpB")
    with pytest.raises(PermissionDeniedError):
        store.delete(attacker, ref)  # another group's artifact
    assert store.read_detail(owner, ref) == [{"x": 1}]  # untouched
