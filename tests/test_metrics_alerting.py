"""Slice 3 — metric failures open incidents that alert configured channels; file capture;
scheduled metric runs. No mocks: local-mode recorder captures the delivery."""

from __future__ import annotations

import csv

API = "/api/v1"


def _conn(client) -> str:
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "m", "auth_method": "none", "coordinates": {"tables": {}}},
    ).json()["data"]["id"]


def _metric(client, cid, query="SELECT {{run_date}} AS v") -> str:
    return client.post(
        f"{API}/metrics",
        json={"name": "rev", "source_type": "sql", "connection_id": cid,
              "source_config": {"query": query}},
    ).json()["data"]["id"]


def _run(client, mid, run_id, value):
    return client.post(f"{API}/metrics/{mid}:run", json={"run_id": run_id, "run_date": str(value)}).json()["data"]


def test_metric_failure_opens_incident_and_notifies(client):
    # A channel + a route so incidents fan out (reuses the integrations we built).
    integ = client.post(f"{API}/integrations", json={
        "provider": "slack", "name": "dq", "values": {"webhook_url": "https://hooks.example/x"}}).json()["data"]
    client.post(f"{API}/integrations/routes", json={
        "event": "incident_opened", "min_severity": "warning", "integration_id": integ["id"]})

    cid = _conn(client)
    mid = _metric(client, cid)
    client.post(f"{API}/metrics/{mid}/rules", json={
        "name": "±5%", "type": "deviation", "severity": "fail",
        "config": {"aggregate": "avg", "tolerance_pct": 5, "min_history": 3}})
    for i in range(3):
        _run(client, mid, f"r{i}", 100)
    assert _run(client, mid, "boom", 120)["observations"][0]["status"] == "fail"

    incidents = client.get(f"{API}/incidents").json()["data"]
    assert any(i["dimension"] == "quality" for i in incidents)
    dels = client.get(f"{API}/integrations/{integ['id']}/deliveries").json()["data"]
    assert any(d["event"] == "incident_opened" and d["status"] == "sent" for d in dels)


def test_pass_resolves_the_incident(client):
    cid = _conn(client)
    mid = _metric(client, cid)
    client.post(f"{API}/metrics/{mid}/rules", json={
        "name": "±5%", "type": "deviation", "severity": "fail",
        "config": {"aggregate": "avg", "tolerance_pct": 5, "min_history": 3}})
    for i in range(3):
        _run(client, mid, f"r{i}", 100)
    _run(client, mid, "boom", 120)   # opens
    _run(client, mid, "ok", 101)     # back in band → resolve
    active = [i for i in client.get(f"{API}/incidents?active_only=true").json()["data"]
              if i.get("kind") == "baseline"]
    assert active == []


def test_capture_error_does_not_open_incident(client):
    cid = _conn(client)
    # query references a table that doesn't exist → capture fails → Error, not Fail (FR-S6)
    mid = _metric(client, cid, query="SELECT v FROM NO_SUCH_TABLE")
    client.post(f"{API}/metrics/{mid}/rules", json={
        "name": "±5%", "type": "deviation", "severity": "fail", "config": {"tolerance_pct": 5}})
    r = _run(client, mid, "r0", 1)
    assert r["observations"][0]["status"] == "error"
    assert [i for i in client.get(f"{API}/incidents").json()["data"] if i.get("kind") == "baseline"] == []


def test_file_metric_row_count(client, tmp_path):
    f = tmp_path / "claims.csv"
    with open(f, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ID", "AMOUNT"])
        for i in range(7):
            w.writerow([i, i * 10])
    cid = client.post(f"{API}/connections", json={
        "source_type": "file", "name": "files", "auth_method": "none",
        "coordinates": {"tables": {"CLAIMS": {"uri": str(f), "format": "csv"}}}}).json()["data"]["id"]
    mid = client.post(f"{API}/metrics", json={
        "name": "claims_rows", "source_type": "file", "connection_id": cid,
        "source_config": {"table": "CLAIMS", "extraction": {"method": "row_count"}}}).json()["data"]["id"]
    r = client.post(f"{API}/metrics/{mid}:run", json={"run_id": "f1"}).json()["data"]
    assert r["observations"][0]["value"] == 7

    # sum extraction on a column
    mid2 = client.post(f"{API}/metrics", json={
        "name": "claims_total", "source_type": "file", "connection_id": cid,
        "source_config": {"table": "CLAIMS", "extraction": {"method": "sum", "column": "AMOUNT"}}}).json()["data"]["id"]
    r2 = client.post(f"{API}/metrics/{mid2}:run", json={"run_id": "f2"}).json()["data"]
    assert r2["observations"][0]["value"] == 210  # 0+10+...+60


def test_scheduled_metric_runs_due(container, ctx, settings):
    # A metric with a cron schedule is picked up by the scheduler's run_once. Built through the
    # same container the Scheduler uses (the HTTP client has its own separate app container/DB).
    from datetime import UTC, datetime

    from dvp.observability.metrics import MetricService
    from dvp.observability.scheduler import Scheduler
    from dvp.services.connections import ConnectionService

    svc = MetricService(container.metadata, container.secrets)
    cid = ConnectionService(container.metadata, container.secrets).create(
        ctx, source_type="local", name="m", coordinates={"tables": {}}, auth_method="none")["id"]
    mid = svc.create(ctx, name="sched", source_type="sql", connection_id=cid,
                     source_config={"query": "SELECT 42 AS v"}, schedule_cron="* * * * *")["id"]

    ran = Scheduler(container, settings, ctx).run_once(now=datetime(2026, 10, 18, 6, 0, tzinfo=UTC))
    assert ran >= 1
    obs = svc.observations(ctx, mid)
    assert any(o["value"] == 42 for o in obs)
