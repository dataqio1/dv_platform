"""Incident triage: comments (who/when), disposition (okay/false), reopen, and excluding a
wrong data point from charts/baselines (double-load / missed-load). No mocks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dvp.db.models import Metric
from dvp.observability.incidents import IncidentService
from dvp.observability.observability_service import TableObservabilityService
from dvp.observability.service import MonitorService
from dvp.services.connections import ConnectionService

API = "/api/v1"


def _incident(container, ctx) -> str:
    return IncidentService(container.metadata).open_or_update(
        ctx, monitor_id=None, dedup_key="t:1", kind="anomaly", dimension="volume",
        severity="warning", summary="Volume spike",
    )["id"]


def test_comment_captures_who_and_when(client, container, ctx):
    iid = _incident(container, ctx)
    svc = IncidentService(container.metadata)
    c = svc.add_comment(ctx, iid, "Looks like a double load")
    assert c["body"] == "Looks like a double load"
    assert c["user_id"] == ctx.user_id
    assert c["author_email"]  # who
    assert c["ts"]            # when
    listed = svc.comments(ctx, iid)
    assert len(listed) == 1 and listed[0]["id"] == c["id"]


def test_resolve_with_disposition_ok_and_false(client, container, ctx):
    svc = IncidentService(container.metadata)
    a = _incident(container, ctx)
    r = svc.resolve(ctx, a, resolution="ok", note="expected backfill")
    assert r["status"] == "resolved" and r["resolution"] == "ok" and r["resolved_by"] == ctx.user_id
    # the note became a comment
    assert any("backfill" in c["body"] for c in svc.comments(ctx, a))

    # different incident → false positive
    b = IncidentService(container.metadata).open_or_update(
        ctx, monitor_id=None, dedup_key="t:2", kind="anomaly", dimension="volume",
        severity="warning", summary="Another",
    )["id"]
    r2 = svc.resolve(ctx, b, resolution="false_positive")
    assert r2["resolution"] == "false_positive"


def test_invalid_resolution_rejected(client, container, ctx):
    iid = _incident(container, ctx)
    import pytest

    from dvp.core.errors import ValidationError
    with pytest.raises(ValidationError):
        IncidentService(container.metadata).resolve(ctx, iid, resolution="whatever")


def test_reopen_clears_resolution(client, container, ctx):
    svc = IncidentService(container.metadata)
    iid = _incident(container, ctx)
    svc.resolve(ctx, iid, resolution="ok")
    r = svc.reopen(ctx, iid)
    assert r["status"] == "open" and r["resolution"] is None and r["resolved_by"] is None


def test_exclude_point_removes_it_from_history(client, container, ctx):
    from dvp.observability import pillars as P

    # a real monitor + connection (get_pillar_history loads the monitor)
    cid = ConnectionService(container.metadata, container.secrets).create(
        ctx, source_type="local", name="c", coordinates={"tables": {}}, auth_method="none")["id"]
    mon = MonitorService(container.metadata).create(
        ctx, connection_id=cid, name="vol", scope={"schema": "PUBLIC", "tables": ["T"]})
    mon_id = mon["id"]

    # seed daily volume with one wild double-load spike
    ids = []
    with container.metadata.session(ctx) as s:
        base = datetime(2026, 10, 1, tzinfo=UTC)
        for i, v in enumerate([100, 100, 100, 100_000, 100]):
            m = Metric(
                org_id=ctx.org_id, group_id=ctx.group_id, ts=base + timedelta(days=i),
                table_ref="PUBLIC.T", name=P.M_VOLUME_ROWS, value=float(v),
                tags={"monitor_id": mon_id, "column": None},
            )
            s.add(m)
            s.flush()
            ids.append(m.id)

    svc = TableObservabilityService(container.metadata)
    hist = svc.get_pillar_history(ctx, mon_id, "PUBLIC.T", "volume")
    assert len(hist["points"]) == 5
    assert any(p["value"] == 100_000 for p in hist["points"])

    # user says "this is wrong — remove it"
    spike_id = ids[3]
    assert svc.set_point_excluded(ctx, spike_id, True)["excluded"] is True

    hist2 = svc.get_pillar_history(ctx, mon_id, "PUBLIC.T", "volume")
    assert len(hist2["points"]) == 4
    assert not any(p["value"] == 100_000 for p in hist2["points"])

    # and it can be restored (keep it)
    svc.set_point_excluded(ctx, spike_id, False)
    assert len(svc.get_pillar_history(ctx, mon_id, "PUBLIC.T", "volume")["points"]) == 5


def test_comment_and_resolve_via_api(client):
    # Drive an incident through a failing compare, then triage it over HTTP.
    def conn(name, n):
        return client.post(f"{API}/connections", json={
            "source_type": "local", "name": name, "auth_method": "none",
            "coordinates": {"schema": "PUBLIC", "tables": {"ORDERS": {
                "columns": [{"name": "ID", "data_type": "NUMBER"}],
                "rows": [{"ID": i} for i in range(n)]}}}}).json()["data"]["id"]
    src, dst = conn("s", 3), conn("d", 2)
    job = client.post(f"{API}/compares", json={
        "kind": "count", "name": "c",
        "source": {"connection_id": src, "schema": "PUBLIC", "table": "ORDERS"},
        "target": {"connection_id": dst, "schema": "PUBLIC", "table": "ORDERS"}}).json()["data"]
    client.post(f"{API}/compares/{job['id']}:run")
    inc = client.get(f"{API}/incidents").json()["data"][0]

    cm = client.post(f"{API}/incidents/{inc['id']}/comments", json={"body": "checked — real issue"})
    assert cm.status_code == 201
    got = client.get(f"{API}/incidents/{inc['id']}/comments").json()["data"]
    assert got[0]["body"] == "checked — real issue" and got[0]["author_email"]

    r = client.post(f"{API}/incidents/{inc['id']}:resolve", json={"resolution": "confirmed", "note": "fixing upstream"})
    assert r.json()["data"]["resolution"] == "confirmed"
    assert len(client.get(f"{API}/incidents/{inc['id']}/comments").json()["data"]) == 2  # note added
