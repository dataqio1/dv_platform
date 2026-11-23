"""Find Column index is PER-USER: one user's index must not surface in another user's search,
even in the same group (connections stay group-shared; test cases & observability are separate)."""

from __future__ import annotations

from dvp.core.tenancy import Role
from dvp.db.models import Membership, User
from dvp.services.connections import ConnectionService
from dvp.services.finder import FinderService
from dvp.services.finder_index import FinderIndexService
from dvp.services.tenancy import resolve_context

COORDS = {
    "database": "RAW", "schema": "PUBLIC",
    "tables": {"T": {"columns": [{"name": "EMAIL", "data_type": "STRING"}],
                     "rows": [{"EMAIL": "a@b.com"}]}},
}


def _second_user_in_same_group(container, ctx):
    with container.metadata.session(ctx) as s:
        u = User(email="userb@example.com", display_name="B")
        s.add(u)
        s.flush()
        s.add(Membership(group_id=ctx.group_id, user_id=u.id, role=Role.EDITOR.value))
        s.flush()
        uid = u.id
    return resolve_context(container.metadata, user_id=uid, org_id=ctx.org_id, group_id=ctx.group_id)


def test_index_and_search_are_per_user(container, ctx):
    store, secrets = container.metadata, container.secrets
    ctx_b = _second_user_in_same_group(container, ctx)

    # A group-shared connection (both users can access it).
    cid = ConnectionService(store, secrets).create(
        ctx, source_type="local", name="c", coordinates=COORDS, auth_method="none",
    )["id"]

    # User A builds the index.
    FinderIndexService(store, secrets).build(ctx, connection_id=cid)
    idx = FinderIndexService(store, secrets)
    finder = FinderService(store, secrets)

    # A sees their own index.
    assert idx.status(ctx, cid)["indexed"] is True
    ra = finder.search(ctx, connection_id=cid, values=["a@b.com"], schema="PUBLIC")
    assert ra["used_index"] is True and ra["hits"]

    # B does NOT see A's index — status shows not-indexed and B's search never uses A's index.
    assert idx.status(ctx_b, cid)["indexed"] is False
    rb = finder.search(ctx_b, connection_id=cid, values=["a@b.com"], schema="PUBLIC")
    assert rb["used_index"] is False  # fell back to live scan; A's Bloom/profile were invisible

    # B building their own index is independent and doesn't disturb A's.
    FinderIndexService(store, secrets).build(ctx_b, connection_id=cid)
    assert idx.status(ctx_b, cid)["indexed"] is True
    assert idx.status(ctx, cid)["indexed"] is True
    assert finder.search(ctx_b, connection_id=cid, values=["a@b.com"], schema="PUBLIC")["used_index"] is True
