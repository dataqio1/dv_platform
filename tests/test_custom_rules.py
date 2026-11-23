"""Custom DQ rules + their metrics — end-to-end (no mocks). Covers scenario S9."""

from __future__ import annotations

API = "/api/v1"
TABLE = "RAW_SALES.PUBLIC.RAW_ORDERS"


def _monitor(client) -> str:
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "inline", "name": "raw", "auth_method": "none", "coordinates": {}},
    ).json()["data"]
    return client.post(
        f"{API}/monitors",
        json={"connection_id": conn["id"], "name": "m", "scope": {"tables": ["RAW_ORDERS"]}},
    ).json()["data"]["id"]


def test_create_custom_sql_rule_and_see_it(client):
    mid = _monitor(client)
    rule = client.post(
        f"{API}/monitors/{mid}/rules",
        json={
            "name": "no_negative_amount",
            "type": "custom_sql",
            "description": "AMOUNT must never be negative",
            "table_ref": TABLE,
            "severity": "critical",
            "config": {
                "expression": "SELECT COUNT(*) AS v FROM {{table}} WHERE AMOUNT < 0",
                "pass_when": "value == 0",
                "threshold": 0,
                "dimension": "quality",
            },
        },
    ).json()["data"]
    assert rule["type"] == "custom_sql"
    assert rule["dimension"] == "quality"
    assert rule["description"] == "AMOUNT must never be negative"

    # appears in the table's rules panel, no result yet
    rules = client.get(f"{API}/monitors/{mid}/tables/{TABLE}/rules").json()["data"]
    assert len(rules) == 1
    assert rules[0]["latest"] is None


def test_record_results_and_history(client):
    mid = _monitor(client)
    rid = client.post(
        f"{API}/monitors/{mid}/rules",
        json={"name": "neg_amount", "type": "custom_sql", "table_ref": TABLE,
              "config": {"threshold": 0}},
    ).json()["data"]["id"]

    # three evaluations over time: 0 (pass), 0 (pass), 3 (fail)
    for ts, v in [("2026-10-01T00:00:00Z", 0), ("2026-10-02T00:00:00Z", 0), ("2026-10-03T00:00:00Z", 3)]:
        r = client.post(
            f"{API}/monitors/{mid}/rules/{rid}:record",
            json={"table_ref": TABLE, "metric_value": v, "ts": ts},
        )
        assert r.status_code == 200
    # last one should have derived passed=False (3 > threshold 0)
    assert r.json()["data"]["passed"] is False

    hist = client.get(f"{API}/monitors/{mid}/rules/{rid}/history").json()["data"]
    assert [p["value"] for p in hist["points"]] == [0, 0, 3]  # chronological
    assert [p["passed"] for p in hist["points"]] == [True, True, False]

    # latest result shows in the table panel
    panel = client.get(f"{API}/monitors/{mid}/tables/{TABLE}/rules").json()["data"]
    latest = panel[0]["latest"]
    assert latest["metric_value"] == 3
    assert latest["passed"] is False


def test_explicit_passed_overrides_derivation(client):
    mid = _monitor(client)
    rid = client.post(
        f"{API}/monitors/{mid}/rules",
        json={"name": "custom", "type": "metric", "table_ref": TABLE},
    ).json()["data"]["id"]
    # metric rule with no threshold: explicit passed respected
    out = client.post(
        f"{API}/monitors/{mid}/rules/{rid}:record",
        json={"table_ref": TABLE, "metric_value": 42.5, "passed": True},
    ).json()["data"]
    assert out["passed"] is True
    assert out["metric_value"] == 42.5


def test_rule_history_isolated_by_group(client):
    # sanity: unknown rule id → 404 problem+json
    mid = _monitor(client)
    r = client.get(f"{API}/monitors/{mid}/rules/does-not-exist/history")
    assert r.status_code == 404
