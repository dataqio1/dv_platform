"""PagerDuty gets one page when a measured incident is critical. A warning stays quiet."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dvp.integrations.providers import pagerduty_event

API = "/api/v1"


def _hours_ago(hours: int) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_pagerduty_event_names_the_table_and_collapses_on_the_incident():
    event = pagerduty_event(
        "routing-key",
        "[CRITICAL] Last load on ORDERS was 96h ago",
        "Table: RAW.PUBLIC.ORDERS",
        {"incident_id": "inc-1", "severity": "critical", "table_ref": "RAW.PUBLIC.ORDERS"},
    )
    assert event["event_action"] == "trigger"
    assert event["dedup_key"] == "inc-1"
    assert event["payload"]["source"] == "RAW.PUBLIC.ORDERS"
    assert event["payload"]["severity"] == "critical"
    assert "ORDERS" in event["payload"]["summary"]
    assert event["routing_key"] == "routing-key"


def test_critical_incident_pages_once_and_a_warning_does_not(client):
    specs = client.get(f"{API}/integrations/provider-specs").json()["data"]
    pager = next(p for p in specs["providers"] if p["provider"] == "pagerduty")
    assert any(f["key"] == "routing_key" and f["secret"] for f in pager["fields"])

    integ = client.post(
        f"{API}/integrations",
        json={
            "provider": "pagerduty",
            "name": "on-call",
            "values": {"routing_key": "routing-key"},
        },
    ).json()["data"]
    assert integ["has_secret"] is True
    assert "routing_key" not in integ["config"]

    client.post(
        f"{API}/integrations/routes",
        json={
            "event": "incident_opened",
            "min_severity": "critical",
            "integration_id": integ["id"],
        },
    )
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "inline", "name": "c", "auth_method": "none", "coordinates": {}},
    ).json()["data"]["id"]
    mid = client.post(
        f"{API}/monitors",
        json={"connection_id": conn, "name": "orders", "scope": {"tables": ["ORDERS"]}},
    ).json()["data"]["id"]

    late = "RAW.PUBLIC.ORDERS"
    client.post(
        f"{API}/monitors/{mid}/tables/{late}/snapshot",
        json={
            "row_count": 10,
            "last_load_ts": _hours_ago(30),
            "columns": [{"name": "ID", "data_type": "NUMBER"}],
        },
    )
    warning = client.get(f"{API}/monitors/{mid}/tables/{late}/incidents").json()["data"]
    assert any(i["kind"] == "freshness" and i["severity"] == "warning" for i in warning)
    quiet = [
        d
        for d in client.get(f"{API}/integrations/{integ['id']}/deliveries").json()["data"]
        if d["event"] == "incident_opened"
    ]
    assert quiet == []

    broken = "RAW.PUBLIC.PAYMENTS"
    client.post(
        f"{API}/monitors/{mid}/tables/{broken}/snapshot",
        json={
            "row_count": 10,
            "last_load_ts": _hours_ago(96),
            "columns": [{"name": "ID", "data_type": "NUMBER"}],
        },
    )
    client.post(
        f"{API}/monitors/{mid}/tables/{broken}/snapshot",
        json={
            "row_count": 10,
            "last_load_ts": _hours_ago(97),
            "columns": [{"name": "ID", "data_type": "NUMBER"}],
        },
    )
    pages = [
        d
        for d in client.get(f"{API}/integrations/{integ['id']}/deliveries").json()["data"]
        if d["event"] == "incident_opened" and d["status"] == "sent"
    ]
    assert len(pages) == 1
    assert "PAYMENTS" in pages[0]["detail"]["subject"]
