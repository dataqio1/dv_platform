"""Backend for the data_observability admin UI — incident silence, monitor health summary,
role matrix, service accounts, org settings/governance, and audit filters/export.

No mocks: every assertion drives the real FastAPI app over its HTTP surface, against the real
(in-memory SQLite) database the app boots with. API → service → DB → response, end to end.
"""

from __future__ import annotations

API = "/api/v1"
TABLE = "ANALYTICS.PUBLIC.ORDERS"


# ----------------------------- helpers -----------------------------
def _monitor(client, name="m") -> str:
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "inline", "name": "c", "auth_method": "none", "coordinates": {}},
    ).json()["data"]
    return client.post(
        f"{API}/monitors",
        json={"connection_id": conn["id"], "name": name, "scope": {"tables": ["ORDERS"]}},
    ).json()["data"]["id"]


def _open_incident(client, mid) -> dict:
    """Drive a failing custom-SQL rule so the engine opens a real incident."""
    rid = client.post(
        f"{API}/monitors/{mid}/rules",
        json={
            "name": "neg", "type": "custom_sql", "table_ref": TABLE,
            "severity": "critical", "config": {"threshold": 0},
        },
    ).json()["data"]["id"]
    client.post(
        f"{API}/monitors/{mid}/rules/{rid}:record",
        json={"table_ref": TABLE, "metric_value": 5, "ts": "2026-10-01T00:00:00Z"},
    )
    active = client.get(f"{API}/monitors/{mid}/incidents?active_only=true").json()["data"]
    assert active, "expected the failing rule to open an incident"
    return active[0]


# ----------------------------- incident silence (screens 3 & 18) -----------------------------
def test_silence_by_duration_then_unsilence(client):
    mid = _monitor(client)
    inc = _open_incident(client, mid)
    assert inc["silenced"] is False

    res = client.post(
        f"{API}/incidents/{inc['id']}:silence",
        json={"scope": "incident", "duration_hours": 24, "note": "expected backfill",
              "ticket_url": "https://jira/DQ-9"},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["silenced"] is True
    assert data["silence_scope"] == "incident"
    assert data["silenced_until"] is not None
    assert data["ticket_url"] == "https://jira/DQ-9"

    # the silence note is also captured as a comment for the investigation timeline
    comments = client.get(f"{API}/incidents/{inc['id']}/comments").json()["data"]
    assert any("expected backfill" in c["body"] for c in comments)

    # lift it
    lifted = client.post(f"{API}/incidents/{inc['id']}:unsilence").json()["data"]
    assert lifted["silenced"] is False
    assert lifted["silenced_until"] is None


def test_silence_by_absolute_until(client):
    mid = _monitor(client)
    inc = _open_incident(client, mid)
    res = client.post(
        f"{API}/incidents/{inc['id']}:silence",
        json={"until": "2099-01-01T00:00:00Z"},
    )
    assert res.status_code == 200
    assert res.json()["data"]["silenced"] is True


def test_silence_rejects_past_and_missing_deadline(client):
    mid = _monitor(client)
    inc = _open_incident(client, mid)
    past = client.post(f"{API}/incidents/{inc['id']}:silence", json={"until": "2000-01-01T00:00:00Z"})
    assert past.status_code >= 400
    empty = client.post(f"{API}/incidents/{inc['id']}:silence", json={"scope": "incident"})
    assert empty.status_code >= 400
    bad_scope = client.post(f"{API}/incidents/{inc['id']}:silence",
                            json={"scope": "galaxy", "duration_hours": 1})
    assert bad_scope.status_code >= 400


# ----------------------------- monitor health summary (screen 1) -----------------------------
def test_monitor_summary_counts_health_and_incidents(client):
    healthy = _monitor(client, name="healthy-one")
    troubled = _monitor(client, name="troubled-one")
    _open_incident(client, troubled)

    summary = client.get(f"{API}/monitors:summary")
    assert summary.status_code == 200, summary.text
    data = summary.json()["data"]
    assert data["total"] >= 2
    # the troubled monitor's critical incident lands in the "error" bucket
    assert data["health"]["error"] >= 1
    assert data["health"]["healthy"] >= 1
    assert data["incidents"]["active_total"] >= 1
    assert data["monitors_with_active_incidents"] >= 1
    assert set(data["health"]) == {"healthy", "warning", "error", "paused"}
    assert healthy  # (used: ensures a healthy monitor exists)


# ----------------------------- role matrix + service accounts (screens 16 & 17) ----------------
def test_role_permission_matrix(client):
    res = client.get(f"{API}/access/roles")
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert "read" in data["permissions"] and "manage_group" in data["permissions"]
    roles = {r["role"]: r for r in data["roles"]}
    assert roles["viewer"]["matrix"]["read"] is True
    assert roles["viewer"]["matrix"]["manage_group"] is False
    assert roles["group_admin"]["matrix"]["manage_group"] is True


def test_service_account_lifecycle(client):
    created = client.post(f"{API}/access/service-accounts", json={"name": "ci", "role": "editor"})
    assert created.status_code == 201, created.text
    sa = created.json()["data"]
    assert sa["role"] == "editor"
    assert sa["api_key"].startswith("dvp_")          # plaintext returned exactly once
    assert sa["key_prefix"] and sa["key_prefix"] in sa["api_key"]
    first_key = sa["api_key"]

    # listing never leaks the key — only the prefix
    listed = client.get(f"{API}/access/service-accounts").json()["data"]
    row = next(r for r in listed if r["id"] == sa["id"])
    assert "api_key" not in row
    assert row["key_prefix"] == sa["key_prefix"]

    # duplicate name rejected
    assert client.post(f"{API}/access/service-accounts",
                       json={"name": "ci", "role": "viewer"}).status_code >= 400
    # org_admin cannot be granted to a service account
    assert client.post(f"{API}/access/service-accounts",
                       json={"name": "root", "role": "org_admin"}).status_code >= 400

    # rotate → new key, old prefix changes
    rotated = client.post(f"{API}/access/service-accounts/{sa['id']}:rotate").json()["data"]
    assert rotated["api_key"] != first_key

    # delete
    assert client.delete(f"{API}/access/service-accounts/{sa['id']}").status_code == 204
    assert all(r["id"] != sa["id"] for r in client.get(f"{API}/access/service-accounts").json()["data"])


# ----------------------------- org settings / governance (screen 17) -----------------------------
def test_org_settings_roundtrip(client):
    assert client.get(f"{API}/settings/organization").json()["data"]["value"] == {}
    put = client.put(f"{API}/settings/organization",
                     json={"value": {"name": "Acme", "timezone": "UTC"}})
    assert put.status_code == 200, put.text
    assert put.json()["data"]["value"]["name"] == "Acme"
    assert client.get(f"{API}/settings/organization").json()["data"]["value"]["timezone"] == "UTC"


def test_governance_and_auth_settings(client):
    gov = client.put(f"{API}/settings/governance",
                     json={"value": {"max_silence_hours": 72, "audit_retention_days": 365}})
    assert gov.status_code == 200
    assert client.get(f"{API}/settings/governance").json()["data"]["value"]["max_silence_hours"] == 72
    auth = client.put(f"{API}/settings/auth", json={"value": {"session_ttl_minutes": 60}})
    assert auth.status_code == 200
    assert client.get(f"{API}/settings/auth").json()["data"]["value"]["session_ttl_minutes"] == 60


# ----------------------------- audit filters + export (screen 15) -----------------------------
def test_audit_filters_and_csv_export(client):
    mid = _monitor(client)
    inc = _open_incident(client, mid)
    client.post(f"{API}/incidents/{inc['id']}:silence", json={"duration_hours": 1, "note": "n"})

    # filter by action — the silence write is recorded
    silenced = client.get(f"{API}/audit?action=incident.silence").json()["data"]
    assert silenced and all(e["action"] == "incident.silence" for e in silenced)
    assert silenced[0]["entity_type"] == "incident"
    assert silenced[0]["actor"]  # actor resolved to a display name/email

    # a far-future lower bound returns nothing
    assert client.get(f"{API}/audit?since=2099-01-01T00:00:00Z").json()["data"] == []
    # a bad timestamp is a client error, not a 500
    assert client.get(f"{API}/audit?since=not-a-date").status_code >= 400

    # CSV export is downloadable and carries a header row
    exp = client.get(f"{API}/audit/export?action=incident.silence")
    assert exp.status_code == 200
    assert exp.headers["content-type"].startswith("text/csv")
    lines = exp.text.strip().splitlines()
    assert lines[0].split(",")[:3] == ["ts", "action", "entity_type"]
    assert any("incident.silence" in ln for ln in lines[1:])
