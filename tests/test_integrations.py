"""Outbound integrations: channel CRUD, test, routing, and incident→notify (no mocks).

Local mode records deliveries via the LocalNotifier and logs them as NotificationDelivery
rows, so the full dispatch path is exercised and asserted through the real API.
"""

from __future__ import annotations

API = "/api/v1"


def _mk_channel(client, provider="slack", name="alerts", values=None):
    body = {"provider": provider, "name": name, "values": values or {"webhook_url": "https://hooks.example/x"}}
    return client.post(f"{API}/integrations", json=body).json()["data"]


def test_provider_specs_lists_channels(client):
    specs = client.get(f"{API}/integrations/provider-specs").json()["data"]
    providers = {p["provider"] for p in specs["providers"]}
    assert {"email", "slack", "teams", "webhook"} <= providers
    assert "incident_opened" in specs["events"]
    # webhook_url must be marked secret
    slack = next(p for p in specs["providers"] if p["provider"] == "slack")
    assert any(f["key"] == "webhook_url" and f["secret"] for f in slack["fields"])


def test_channel_crud_hides_secret(client):
    integ = _mk_channel(client)
    assert integ["provider"] == "slack"
    assert integ["has_secret"] is True
    assert "webhook_url" not in integ["config"]  # secret, not echoed in config
    # listed for the tenant
    listed = client.get(f"{API}/integrations").json()["data"]
    assert any(i["id"] == integ["id"] for i in listed)
    # update name + disable
    upd = client.patch(f"{API}/integrations/{integ['id']}", json={"name": "renamed", "enabled": False}).json()["data"]
    assert upd["name"] == "renamed" and upd["enabled"] is False
    # delete
    assert client.delete(f"{API}/integrations/{integ['id']}").status_code == 204


def test_test_endpoint_records_delivery(client):
    integ = _mk_channel(client, provider="teams", values={"webhook_url": "https://teams.example/x"})
    res = client.post(f"{API}/integrations/{integ['id']}:test").json()["data"]
    assert res["ok"] is True  # local recorder always succeeds
    dels = client.get(f"{API}/integrations/{integ['id']}/deliveries").json()["data"]
    assert len(dels) == 1 and dels[0]["event"] == "test" and dels[0]["status"] == "sent"


def test_unknown_provider_rejected(client):
    r = client.post(f"{API}/integrations", json={"provider": "carrier_pigeon", "name": "x", "values": {}})
    assert r.status_code >= 400


def test_route_crud(client):
    integ = _mk_channel(client)
    route = client.post(
        f"{API}/integrations/routes",
        json={"event": "incident_opened", "min_severity": "warning", "integration_id": integ["id"]},
    ).json()["data"]
    assert route["min_severity"] == "warning"
    routes = client.get(f"{API}/integrations/routes").json()["data"]
    assert any(r["id"] == route["id"] for r in routes)
    upd = client.patch(f"{API}/integrations/routes/{route['id']}", json={"min_severity": "critical"}).json()["data"]
    assert upd["min_severity"] == "critical"
    assert client.delete(f"{API}/integrations/routes/{route['id']}").status_code == 204


def _open_incident(client, severity="critical"):  # noqa: ARG001
    """Drive a real incident from a compare that found different row counts."""

    def conn(name, n):
        rows = [{"ID": i, "AMOUNT": 10} for i in range(n)]
        return client.post(
            f"{API}/connections",
            json={
                "source_type": "local",
                "name": name,
                "auth_method": "none",
                "coordinates": {
                    "database": "DB",
                    "schema": "PUBLIC",
                    "tables": {
                        "ORDERS": {
                            "columns": [
                                {"name": "ID", "data_type": "NUMBER"},
                                {"name": "AMOUNT", "data_type": "NUMBER"},
                            ],
                            "rows": rows,
                        }
                    },
                },
            },
        ).json()["data"]["id"]

    src, dst = conn("src", 3), conn("dst", 2)
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "count",
            "name": "cmp",
            "source": {"connection_id": src, "schema": "PUBLIC", "table": "ORDERS"},
            "target": {"connection_id": dst, "schema": "PUBLIC", "table": "ORDERS"},
        },
    ).json()["data"]
    client.post(f"{API}/compares/{job['id']}:run")


def test_incident_opens_and_notifies_matching_channel(client):
    integ = _mk_channel(client, provider="slack")
    client.post(f"{API}/integrations/routes", json={
        "event": "incident_opened", "min_severity": "warning", "integration_id": integ["id"]
    })
    _open_incident(client)

    # An incident exists and the channel received exactly one delivery for it.
    incidents = client.get(f"{API}/incidents").json()["data"]
    assert len(incidents) >= 1
    dels = client.get(f"{API}/integrations/{integ['id']}/deliveries").json()["data"]
    opened = [d for d in dels if d["event"] == "incident_opened" and d["status"] == "sent"]
    assert len(opened) == 1


def test_route_severity_gate_blocks_lower_severity(client):
    # A count-mismatch compare opens a *warning* incident; a critical-only route must not fire.
    integ = _mk_channel(client, provider="webhook", values={"url": "https://hook.example/x"})
    client.post(f"{API}/integrations/routes", json={
        "event": "incident_opened", "min_severity": "critical", "integration_id": integ["id"]
    })
    _open_incident(client)
    # The incident sits below the route's 'critical' floor, so nothing is delivered.
    incidents = client.get(f"{API}/incidents").json()["data"]
    assert any(i["severity"] == "warning" for i in incidents)
    dels = client.get(f"{API}/integrations/{integ['id']}/deliveries").json()["data"]
    opened = [d for d in dels if d["event"] == "incident_opened"]
    assert opened == []
