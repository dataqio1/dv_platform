"""Historical-baseline metrics end-to-end (no mocks): SQL capture → history → evaluate.

Uses a `local` connection and a metric whose query returns the run_date literal, so the test
fully controls the value series and can drive Learning → Pass → Fail deterministically.
"""

from __future__ import annotations

API = "/api/v1"


def _conn(client) -> str:
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "m", "auth_method": "none",
              "coordinates": {"tables": {}}},
    ).json()["data"]["id"]


def _metric(client, cid) -> str:
    # value == run_date, so the test controls each captured value
    return client.post(
        f"{API}/metrics",
        json={"name": "rev", "source_type": "sql", "connection_id": cid,
              "source_config": {"query": "SELECT {{run_date}} AS v"}},
    ).json()["data"]["id"]


def _run(client, mid, run_id, value):
    return client.post(f"{API}/metrics/{mid}:run", json={"run_id": run_id, "run_date": str(value)}).json()["data"]


def test_capture_learning_then_pass_then_fail(client):
    cid = _conn(client)
    mid = _metric(client, cid)
    client.post(f"{API}/metrics/{mid}/rules", json={
        "name": "±5% of avg", "type": "deviation", "severity": "fail",
        "config": {"aggregate": "avg", "window": {"observations": 30}, "tolerance_pct": 5},
    })

    # 5 captures of 100 — each sees < 5 prior valid obs → Learning (but still recorded)
    for i in range(5):
        r = _run(client, mid, f"r{i}", 100)
        assert r["observations"][0]["status"] == "learning"

    # 6th sees 5 valid history → baseline avg 100 → 100 passes
    assert _run(client, mid, "r5", 100)["observations"][0]["status"] == "pass"
    # a 6.9% jump fails the ±5% rule
    assert _run(client, mid, "r6", 106)["observations"][0]["status"] == "fail"
    assert _run(client, mid, "r7", 104)["observations"][0]["status"] == "pass"

    # history is stored and newest-first
    obs = client.get(f"{API}/metrics/{mid}/observations").json()["data"]
    assert len(obs) == 8
    assert obs[0]["value"] == 104


def test_rerun_same_run_id_does_not_duplicate(client):
    cid = _conn(client)
    mid = _metric(client, cid)
    _run(client, mid, "run-A", 100)
    _run(client, mid, "run-A", 100)  # FR-H5: same run id must not create a duplicate
    obs = client.get(f"{API}/metrics/{mid}/observations").json()["data"]
    assert len([o for o in obs if o["run_id"] == "run-A"]) == 1


def test_exclude_keeps_it_out_of_the_baseline(client):
    cid = _conn(client)
    mid = _metric(client, cid)
    client.post(f"{API}/metrics/{mid}/rules", json={
        "name": "±5%", "type": "deviation", "severity": "fail",
        "config": {"aggregate": "avg", "tolerance_pct": 5, "min_history": 3},
    })
    for i in range(3):
        _run(client, mid, f"r{i}", 100)
    # a huge outlier would wreck the baseline…
    _run(client, mid, "spike", 10_000)
    # …so exclude it, then a normal 101 must pass against the 100-baseline
    spike_id = next(o["id"] for o in client.get(f"{API}/metrics/{mid}/observations").json()["data"] if o["run_id"] == "spike")
    client.post(f"{API}/metrics/observations/{spike_id}:exclude", json={"reason": "load test"})
    assert _run(client, mid, "after", 101)["observations"][0]["status"] == "pass"


def test_monotonic_reset(client):
    cid = _conn(client)
    mid = _metric(client, cid)
    client.post(f"{API}/metrics/{mid}/rules", json={
        "name": "non-decreasing", "type": "monotonic", "severity": "fail",
        "config": {"direction": "non_decreasing"},
    })
    _run(client, mid, "r1", 1000)
    assert _run(client, mid, "r2", 990)["observations"][0]["status"] == "fail"
    # declare a reset — history before it no longer counts, so 990 passes
    client.post(f"{API}/metrics/{mid}:reset", json={"reset_at": "2099-01-01", "reason": "truncate+reload"})
    assert _run(client, mid, "r3", 990)["observations"][0]["status"] == "pass"


def test_unsupported_rule_type_rejected(client):
    cid = _conn(client)
    mid = _metric(client, cid)
    r = client.post(f"{API}/metrics/{mid}/rules", json={"name": "x", "type": "statistical", "config": {}})
    assert r.status_code >= 400  # Phase 2
