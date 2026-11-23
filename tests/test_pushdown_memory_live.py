"""The scheduler charges a PUSHDOWN compare (Snowflake hash / in-database) only a small memory
footprint, so many can run concurrently — while an in-memory compare reserves its full budget.
Real DB + real CompareService, no mocks."""

from __future__ import annotations

from dvp.db.models import CompareJob, Connection
from dvp.testing_engine.service import _PUSHDOWN_MEMORY_GB, CompareService


def _ctx(client):
    container = client.app.state.container
    local = client.app.state.local_tenant
    from dvp.services.tenancy import resolve_context

    return resolve_context(
        container.metadata, user_id=local.user_id, org_id=local.org_id, group_id=local.group_id
    )


def _svc(client) -> CompareService:
    c = client.app.state.container
    return CompareService(c.metadata, c.results, c.secrets, c.spark)


def _conn(client, ctx, source_type: str) -> str:
    store = client.app.state.container.metadata
    with store.session(ctx) as s:
        c = Connection(
            org_id=ctx.org_id, group_id=ctx.group_id, source_type=source_type,
            name=f"{source_type}-conn", auth_method="none", coordinates={}, status="active",
        )
        s.add(c)
        s.flush()
        return c.id


def _job(client, ctx, src_conn: str, dst_conn: str, options: dict) -> str:
    store = client.app.state.container.metadata
    with store.session(ctx) as s:
        j = CompareJob(
            org_id=ctx.org_id, group_id=ctx.group_id, kind="data", name="j",
            source={"connection_id": src_conn, "table": "T"},
            target={"connection_id": dst_conn, "table": "T"},
            options=options, status="created",
        )
        s.add(j)
        s.flush()
        return j.id


def test_snowflake_to_snowflake_exact_is_charged_pushdown_memory(client):
    ctx = _ctx(client)
    sf1, sf2 = _conn(client, ctx, "snowflake"), _conn(client, ctx, "snowflake")
    # Even with a big memory_gb budget, an exact SF↔SF compare pushes down → tiny footprint.
    jid = _job(client, ctx, sf1, sf2, {"key": ["ID"], "memory_gb": 16})
    mem, cpu = _svc(client).mark_queued(ctx, jid)
    assert mem == _PUSHDOWN_MEMORY_GB
    assert cpu == 1


def test_tolerance_forces_in_memory_budget(client):
    """A numeric tolerance can't be a hash equality → in-memory → full budget reserved."""
    ctx = _ctx(client)
    sf1, sf2 = _conn(client, ctx, "snowflake"), _conn(client, ctx, "snowflake")
    jid = _job(client, ctx, sf1, sf2, {"key": ["ID"], "memory_gb": 8, "tolerance": 2.0})
    mem, _ = _svc(client).mark_queued(ctx, jid)
    assert mem == 8.0


def test_forced_inmemory_engine_reserves_full_budget(client):
    ctx = _ctx(client)
    sf1, sf2 = _conn(client, ctx, "snowflake"), _conn(client, ctx, "snowflake")
    jid = _job(client, ctx, sf1, sf2, {"key": ["ID"], "memory_gb": 8, "engine": "inmemory"})
    mem, _ = _svc(client).mark_queued(ctx, jid)
    assert mem == 8.0


def test_non_snowflake_side_reserves_full_budget(client):
    """One side not Snowflake → can't push down → in-memory → full budget (default 1 GB)."""
    ctx = _ctx(client)
    sf, local = _conn(client, ctx, "snowflake"), _conn(client, ctx, "local")
    jid = _job(client, ctx, sf, local, {"key": ["ID"]})
    mem, cpu = _svc(client).mark_queued(ctx, jid)
    assert mem == 1.0 and cpu == 1
