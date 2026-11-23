"""Regression alerting: when a passing test case starts differing/failing, an incident opens
(escalated when it was previously passing); when it recovers, the incident auto-resolves.
Real API + real IncidentService, no mocks."""

from __future__ import annotations

from dvp.db.models import CompareJob

API = "/api/v1"

COLS = [{"name": "ORDER_ID", "data_type": "NUMBER"}, {"name": "AMOUNT", "data_type": "NUMBER"}]


def _conn(client, name, amount_a=10):
    """A local connection whose ORDERS.AMOUNT for ORDER_ID=1 is `amount_a` (so two conns can
    differ on demand)."""
    coords = {
        "database": "DB", "schema": "PUBLIC",
        "tables": {"ORDERS": {"columns": COLS, "rows": [
            {"ORDER_ID": 1, "AMOUNT": amount_a}, {"ORDER_ID": 2, "AMOUNT": 20},
        ]}},
    }
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": name, "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]


def _set_amount(client, conn_id: str, amount: int) -> None:
    """Mutate the stored inline table so the next run sees different data (simulating drift)."""
    store = client.app.state.container.metadata
    from dvp.db.models import Connection

    with store.session(None) as s:
        c = s.get(Connection, conn_id)
        coords = dict(c.coordinates)
        tables = dict(coords["tables"])
        orders = dict(tables["ORDERS"])
        orders["rows"] = [{"ORDER_ID": 1, "AMOUNT": amount}, {"ORDER_ID": 2, "AMOUNT": 20}]
        tables["ORDERS"] = orders
        coords["tables"] = tables
        c.coordinates = coords
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(c, "coordinates")


def test_pass_then_diff_opens_regression_then_recovery_resolves(client):
    src = _conn(client, "reg-src", amount_a=10)
    dst = _conn(client, "reg-dst", amount_a=10)  # identical → first run passes
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "data", "name": "amounts",
            "source": {"connection_id": src, "table": "ORDERS"},
            "target": {"connection_id": dst, "table": "ORDERS"},
            "options": {"key": ["ORDER_ID"]},
        },
    ).json()["data"]["id"]

    # Run 1 — clean pass. No incident.
    client.post(f"{API}/compares/{job}:run")
    incs = client.get(f"{API}/incidents").json()["data"]
    assert not any(i["summary"].startswith("Compare amounts") for i in incs)

    # Drift the target so run 2 differs — a regression (was passing).
    _set_amount(client, dst, 999)
    client.post(f"{API}/compares/{job}:run")
    incs = client.get(f"{API}/incidents").json()["data"]
    reg = [i for i in incs if i["summary"].startswith("Compare amounts")]
    assert len(reg) == 1, incs
    assert "regressed" in reg[0]["summary"]
    assert reg[0]["severity"] == "critical"  # pass→diff escalates
    assert reg[0]["status"] in ("open", "acknowledged")
    assert reg[0]["evidence"].get("regression") is True

    # Fix the drift so run 3 passes again — the incident auto-resolves (recovery).
    _set_amount(client, dst, 10)
    client.post(f"{API}/compares/{job}:run")
    incs = client.get(f"{API}/incidents").json()["data"]
    reg = [i for i in incs if i["summary"].startswith("Compare amounts")]
    assert len(reg) == 1 and reg[0]["status"] == "resolved"


def test_first_run_difference_is_warning_not_regression(client):
    """A compare that differs on its FIRST run has no prior pass — it's a warning, not a regression."""
    src = _conn(client, "first-src", amount_a=10)
    dst = _conn(client, "first-dst", amount_a=777)  # differs immediately
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "data", "name": "firstdiff",
            "source": {"connection_id": src, "table": "ORDERS"},
            "target": {"connection_id": dst, "table": "ORDERS"},
            "options": {"key": ["ORDER_ID"]},
        },
    ).json()["data"]["id"]

    client.post(f"{API}/compares/{job}:run")
    incs = client.get(f"{API}/incidents").json()["data"]
    inc = next(i for i in incs if i["summary"].startswith("Compare firstdiff"))
    assert inc["severity"] == "warning"
    assert "regressed" not in inc["summary"]
    assert inc["evidence"].get("regression") is False


def test_pass_then_execution_failure_opens_incident(client):
    """A compare that passed, then can't run (bad table), opens a 'stopped running' incident."""
    src = _conn(client, "ex-src", amount_a=10)
    dst = _conn(client, "ex-dst", amount_a=10)
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "data", "name": "execreg",
            "source": {"connection_id": src, "table": "ORDERS"},
            "target": {"connection_id": dst, "table": "ORDERS"},
            "options": {"key": ["ORDER_ID"]},
        },
    ).json()["data"]["id"]
    client.post(f"{API}/compares/{job}:run")  # pass

    # Point the target at a missing table so the next run fails.
    store = client.app.state.container.metadata
    with store.session(None) as s:
        j = s.get(CompareJob, job)
        tgt = dict(j.target)
        tgt["table"] = "GONE"
        j.target = tgt
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(j, "target")

    client.post(f"{API}/compares/{job}:run")  # fails
    incs = client.get(f"{API}/incidents").json()["data"]
    inc = next((i for i in incs if i["summary"].startswith("Compare execreg stopped running")), None)
    assert inc is not None and inc["status"] in ("open", "acknowledged")
