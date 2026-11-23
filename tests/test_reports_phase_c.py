"""Phase C reports: PDF export, profile report, and scheduled delivery (real, no mocks)."""

from __future__ import annotations

from datetime import datetime

import pytest

API = "/api/v1"

TBL = {
    "columns": [
        {"name": "ORDER_ID", "data_type": "NUMBER"},
        {"name": "AMOUNT", "data_type": "NUMBER"},
        {"name": "STATUS", "data_type": "VARCHAR"},
    ],
    "rows": [
        {"ORDER_ID": 1, "AMOUNT": 10, "STATUS": "OPEN"},
        {"ORDER_ID": 2, "AMOUNT": 20, "STATUS": None},
        {"ORDER_ID": 3, "AMOUNT": 10, "STATUS": "OPEN"},
    ],
}


def _conn(client):
    coords = {"database": "DB", "schema": "PUBLIC", "tables": {"ORDERS": TBL}}
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "src", "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]


def _data_job(client):
    s = _conn(client)
    t = _conn(client)
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "data", "name": "cmp",
            "source": {"connection_id": s, "table": "ORDERS"},
            "target": {"connection_id": t, "table": "ORDERS"},
            "options": {"key": ["ORDER_ID"]},
        },
    ).json()["data"]
    client.post(f"{API}/compares/{job['id']}:run")


# ----------------------------- PDF export -----------------------------
def test_pdf_export(client):
    _data_job(client)
    rep = client.post(f"{API}/reports", json={"name": "R", "report_type": "data_validation"}).json()["data"]
    client.post(f"{API}/reports/{rep['id']}:generate")
    r = client.get(f"{API}/reports/{rep['id']}/export?fmt=pdf")
    assert r.status_code == 200
    assert "application/pdf" in r.headers["content-type"]
    assert r.content[:5] == b"%PDF-"


# ----------------------------- profile report -----------------------------
def test_profile_report(client):
    cid = _conn(client)
    rep = client.post(
        f"{API}/reports",
        json={
            "name": "Profile ORDERS", "report_type": "profile",
            "spec": {"connection_id": cid, "schema": "PUBLIC", "tables": ["ORDERS"]},
        },
    ).json()["data"]
    gen = client.post(f"{API}/reports/{rep['id']}:generate").json()["data"]
    assert gen["summary"]["tables_profiled"] == 1
    assert gen["summary"]["total_rows"] == 3

    doc = client.get(f"{API}/reports/{rep['id']}").json()["data"]["document"]
    tbl = doc["tables"][0]
    cols = {c["name"]: c for c in tbl["columns"]}
    assert cols["ORDER_ID"]["distinct"] == 3
    assert cols["ORDER_ID"]["null_pct"] == 0.0
    assert cols["ORDER_ID"]["distinct_pct"] == 100.0  # fully unique key
    assert cols["STATUS"]["null_count"] == 1  # one NULL status
    assert cols["AMOUNT"]["min"] in (10, "10")
    # local SQLite has no approx-distinct → exact
    assert cols["ORDER_ID"]["distinct_approx"] is False
    # empty-string capture (none here, field present)
    assert cols["STATUS"]["empty_count"] == 0


# ----------------------------- scheduled delivery -----------------------------
def _ctx_and_container(client):
    from dvp.services.tenancy import resolve_context

    c = client.app.state.container
    lt = client.app.state.local_tenant
    ctx = resolve_context(c.metadata, user_id=lt.user_id, org_id=lt.org_id, group_id=lt.group_id)
    return ctx, c


def test_deliver_sends_digest(client):
    _data_job(client)
    rep = client.post(
        f"{API}/reports",
        json={
            "name": "Nightly", "report_type": "data_validation",
            "schedule_cron": "0 7 * * *",
            "delivery": {"email": ["lead@acme.io", "qa@acme.io"], "slack": ["#data-health"]},
        },
    ).json()["data"]

    out = client.post(f"{API}/reports/{rep['id']}:deliver").json()["data"]
    assert len(out["delivered"]) == 3  # 2 email + 1 slack
    # LocalNotifier actually recorded the sends (no mocks)
    sent = client.app.state.container.notifier.sent
    assert {d.channel for d in sent} == {"email", "slack"}
    assert any(d.recipient == "lead@acme.io" for d in sent)
    assert all(d.subject.startswith("[data_validation]") for d in sent)


def test_scheduler_tick_generates_and_delivers_due(client):
    from dvp.reporting.service import ReportService

    _data_job(client)
    # due report (every minute) + a non-scheduled one
    due = client.post(
        f"{API}/reports",
        json={"name": "Due", "report_type": "data_validation", "schedule_cron": "* * * * *",
              "delivery": {"email": ["ops@acme.io"]}},
    ).json()["data"]
    client.post(f"{API}/reports", json={"name": "Unscheduled", "report_type": "data_validation"})

    ctx, c = _ctx_and_container(client)
    svc = ReportService(c.metadata, c.results, c.secrets, c.notifier)
    processed = svc.tick(ctx, datetime(2026, 10, 8, 7, 0))
    assert due["id"] in processed
    # the due report got generated (a version now exists) and delivered
    assert client.get(f"{API}/reports/{due['id']}").json()["data"]["latest_version"] >= 1
    assert any(d.recipient == "ops@acme.io" for d in c.notifier.sent)


def test_profile_uses_approx_distinct_above_threshold(container, ctx):
    """On a warehouse dialect, pass 1 uses APPROX_COUNT_DISTINCT; only low-cardinality
    columns get an exact COUNT(DISTINCT) refine pass. (SQLite can't, so we fake the dialect.)"""
    from dvp.connectors.base import ColumnDef, QueryResult
    from dvp.reporting.service import ReportService

    class _FakeWarehouse:
        dialect = "snowflake"

        def __init__(self):
            self.queries = []

        def sql_ref(self, tref):
            return tref.table

        def get_schema(self, tref):
            return [ColumnDef("ID", "NUMBER", False), ColumnDef("STATUS", "VARCHAR", True)]

        def execute(self, sql):
            self.queries.append(sql)
            if "APPROX_COUNT_DISTINCT" in sql:  # pass 1: ID high-card, STATUS low-card
                return QueryResult(
                    columns=["__n", "c0_nn", "c0_e", "c0_d", "c0_min", "c0_max",
                             "c1_nn", "c1_e", "c1_d", "c1_min", "c1_max"],
                    rows=[[100000, 100000, 0, 98000, 1, 100000, 100000, 0, 3, "A", "C"]],
                )
            return QueryResult(columns=["c1_d"], rows=[[3]])  # exact refine for STATUS only

    conn = _FakeWarehouse()
    svc = ReportService(container.metadata, container.results, container.secrets)
    svc._connector = lambda ctx, cid: conn  # type: ignore[assignment]
    tables = svc._profile_tables(
        ctx, {"connection_id": "x", "tables": ["EVENTS"], "approx_threshold": 1000}
    )
    cols = {c["name"]: c for c in tables[0]["columns"]}
    assert cols["ID"]["distinct"] == 98000 and cols["ID"]["distinct_approx"] is True
    assert cols["STATUS"]["distinct"] == 3 and cols["STATUS"]["distinct_approx"] is False
    # pass 1 approx + exactly one refine pass (only the low-card column)
    assert sum("APPROX_COUNT_DISTINCT" in q for q in conn.queries) == 1
    refine = [q for q in conn.queries if "COUNT(DISTINCT" in q and "APPROX" not in q]
    assert len(refine) == 1 and "c1_d" in refine[0] and "c0_d" not in refine[0]


@pytest.mark.parametrize("fmt", ["json", "csv", "html"])
def test_other_exports_still_work(client, fmt):
    _data_job(client)
    rep = client.post(f"{API}/reports", json={"name": "R", "report_type": "data_validation"}).json()["data"]
    client.post(f"{API}/reports/{rep['id']}:generate")
    assert client.get(f"{API}/reports/{rep['id']}/export?fmt={fmt}").status_code == 200
