"""The /ready readiness probe: 200 when the metadata DB answers, 503 when it doesn't. Distinct
from /health (liveness). Real app via TestClient, no mocks — the failure case is simulated by
making the real session factory raise, not by patching the endpoint."""

from __future__ import annotations

API = "/api/v1"


def test_ready_returns_200_when_db_reachable(client):
    r = client.get(f"{API}/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"


def test_ready_returns_503_when_db_unreachable(client, monkeypatch):
    """If the DB ping raises, the probe reports not_ready with 503 so orchestrators stop routing."""
    metadata = client.app.state.container.metadata

    def _boom(*_a, **_k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(metadata, "session", _boom)
    r = client.get(f"{API}/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == "unavailable"
    # The internal error detail is never exposed to the caller.
    assert "connection refused" not in r.text


def test_ready_needs_no_auth(client):
    """Probe must work without an Authorization header (orchestrators don't send one)."""
    r = client.get(f"{API}/ready", headers={"Authorization": ""})
    assert r.status_code in (200, 503)  # answered, not a 401
