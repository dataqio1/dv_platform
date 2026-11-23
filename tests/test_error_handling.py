"""The API catch-all exception handler: any unexpected error still returns the problem+json
envelope with a quotable error_id, and never leaks the exception message or a stack trace
(golden rule #3). Real app via TestClient — no mocks."""

from __future__ import annotations

from fastapi.testclient import TestClient

from dvp.api.app import create_app

API = "/api/v1"


def _client_with_boom(settings) -> TestClient:
    app = create_app(settings)

    def _boom() -> dict:
        raise RuntimeError("super secret internal detail: db password = hunter2")

    # create_app registers a SPA catch-all (`/{full_path:path}`) last, which would otherwise
    # shadow a route added here — insert ours at the front so the request reaches it.
    app.add_api_route(f"{API}/_boom", _boom, methods=["GET"])
    app.router.routes.insert(0, app.router.routes.pop())

    # raise_server_exceptions=False so the handler runs instead of re-raising into the test.
    return TestClient(app, raise_server_exceptions=False)


def test_unhandled_exception_returns_problem_json(settings):
    with _client_with_boom(settings) as client:
        r = client.get(f"{API}/_boom")

    assert r.status_code == 500
    body = r.json()
    assert body["title"] == "internal_error"
    assert body["status"] == 500
    assert body["type"] == "about:blank#internal_error"
    # A quotable correlation id is surfaced to the caller...
    error_id = body["errors"]["error_id"]
    assert error_id
    # ...and it IS the request's correlation id (also in the response header + log lines).
    assert r.headers["X-Request-ID"] == error_id
    # ...but the internal message / secret is NEVER leaked in the response.
    assert "hunter2" not in r.text
    assert "RuntimeError" not in r.text
    assert "super secret" not in r.text


def test_error_id_reuses_caller_supplied_request_id(settings):
    """A caller-supplied X-Request-ID flows through to the error_id — a trace that spans services."""
    with _client_with_boom(settings) as client:
        r = client.get(f"{API}/_boom", headers={"X-Request-ID": "trace-abc-123"})
    assert r.headers["X-Request-ID"] == "trace-abc-123"
    assert r.json()["errors"]["error_id"] == "trace-abc-123"


def test_typed_dvp_errors_still_mapped(client):
    """The catch-all must not shadow the existing typed-error mapping (404 for NotFound)."""
    r = client.get(f"{API}/connections/does-not-exist")
    assert r.status_code == 404
    assert r.json()["title"] == "not_found"


# --------------------------------------------------------------------------- correlation id


def test_every_response_carries_a_request_id(client):
    r = client.get(f"{API}/health")
    assert r.status_code == 200
    rid = r.headers.get("X-Request-ID")
    assert rid and len(rid) == 12  # minted when the caller supplies none


def test_request_id_is_echoed_when_supplied(client):
    r = client.get(f"{API}/health", headers={"X-Request-ID": "my-trace-id"})
    assert r.headers["X-Request-ID"] == "my-trace-id"
