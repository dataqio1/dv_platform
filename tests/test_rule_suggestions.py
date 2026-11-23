"""Load-pattern rule suggestions → acknowledge (tweak %) → materialized metric + baseline rules."""

from __future__ import annotations

from dvp.services.table_metadata import RULE_SUGGESTIONS, rule_suggestions

API = "/api/v1"


def test_every_load_pattern_has_sane_suggestions():
    # full_snapshot (truncate & load) must suggest a deviation rule, as in the user's example.
    fs = rule_suggestions("full_snapshot")
    assert any(s["type"] == "deviation" for s in fs)
    dev = next(s for s in fs if s["type"] == "deviation")
    assert any(p["key"] == "tolerance_pct" and p["default"] == 5 for p in dev["params"])

    # append-only must suggest "never decreases".
    assert any(s["type"] == "monotonic" and s["config"]["direction"] == "non_decreasing"
               for s in rule_suggestions("append"))
    # unknown → no suggestions (never auto-apply blindly).
    assert rule_suggestions("unknown") == []


def test_rule_suggestions_endpoint(client):
    data = client.get(f"{API}/table-metadata/rule-suggestions?load_pattern=full_snapshot").json()["data"]
    assert data and data[0]["type"] == "deviation"
    # also exposed in the pattern-specs catalog for the UI
    specs = client.get(f"{API}/table-metadata/pattern-specs").json()["data"]
    assert "rule_suggestions" in specs and "append" in specs["rule_suggestions"]


def test_acknowledge_creates_metric_and_rules_with_tweaked_pct(client):
    cid = client.post(f"{API}/connections", json={
        "source_type": "local", "name": "c", "auth_method": "none",
        "coordinates": {"database": "DWH", "schema": "PUBLIC",
                        "tables": {"ORDERS": {"columns": [{"name": "ID", "data_type": "NUMBER"}],
                                              "rows": [{"ID": 1}, {"ID": 2}]}}}}).json()["data"]["id"]
    mon = client.post(f"{API}/monitors", json={
        "connection_id": cid, "name": "obs", "scope": {"schema": "PUBLIC", "tables": ["ORDERS"]}}).json()["data"]

    # User ticks the deviation suggestion but tweaks the tolerance from 5% → 8%.
    # (Unqualified table ref so the in-memory local connector resolves it; real Snowflake uses FQN.)
    ack = client.post(f"{API}/metrics/from-table-suggestions", json={
        "monitor_id": mon["id"], "table_ref": "ORDERS", "connection_id": cid,
        "load_pattern": "full_snapshot",
        "selections": [{"key": "deviation_pct", "params": {"tolerance_pct": 8}}],
    })
    assert ack.status_code == 201
    data = ack.json()["data"]
    assert data["metric"]["name"] == "ORDERS row count"
    assert data["metric"]["monitor_id"] == mon["id"]
    assert len(data["rules"]) == 1
    rule = data["rules"][0]
    assert rule["type"] == "deviation"
    assert rule["config"]["tolerance_pct"] == 8  # the user's tweak won

    # The metric is listed under the monitor and can capture its row count.
    metrics = client.get(f"{API}/metrics?monitor_id={mon['id']}").json()["data"]
    assert any(m["id"] == data["metric"]["id"] for m in metrics)
    run = client.post(f"{API}/metrics/{data['metric']['id']}:run", json={"run_id": "r1"}).json()["data"]
    assert run["observations"][0]["value"] == 2  # two rows in ORDERS


def test_append_acknowledge_adds_monotonic_and_growth(client):
    cid = client.post(f"{API}/connections", json={
        "source_type": "local", "name": "c2", "auth_method": "none",
        "coordinates": {"schema": "PUBLIC",
                        "tables": {"EVENTS": {"columns": [{"name": "ID", "data_type": "NUMBER"}],
                                              "rows": [{"ID": 1}]}}}}).json()["data"]["id"]
    mon = client.post(f"{API}/monitors", json={
        "connection_id": cid, "name": "obs2", "scope": {"schema": "PUBLIC", "tables": ["EVENTS"]}}).json()["data"]
    ack = client.post(f"{API}/metrics/from-table-suggestions", json={
        "monitor_id": mon["id"], "table_ref": "PUBLIC.EVENTS", "connection_id": cid,
        "load_pattern": "append",
        "selections": [{"key": "monotonic_nondec", "params": {}},
                       {"key": "growth_band", "params": {"min_change": 1, "max_change": 500}}],
    }).json()["data"]
    types = {r["type"] for r in ack["rules"]}
    assert types == {"monotonic", "growth"}
    growth = next(r for r in ack["rules"] if r["type"] == "growth")
    assert growth["config"]["max_change"] == 500


def test_no_valid_selection_is_rejected(client):
    cid = client.post(f"{API}/connections", json={
        "source_type": "local", "name": "c3", "auth_method": "none", "coordinates": {"tables": {}}}).json()["data"]["id"]
    mon = client.post(f"{API}/monitors", json={"connection_id": cid, "name": "o3", "scope": {"tables": []}}).json()["data"]
    r = client.post(f"{API}/metrics/from-table-suggestions", json={
        "monitor_id": mon["id"], "table_ref": "T", "connection_id": cid,
        "load_pattern": "full_snapshot", "selections": [{"key": "does_not_exist", "params": {}}]})
    assert r.status_code >= 400


def test_catalog_is_internally_consistent():
    # every suggestion's params must have defaults so the UI can prefill
    for _pattern, suggestions in RULE_SUGGESTIONS.items():
        for s in suggestions:
            assert s["type"] in {"deviation", "monotonic", "growth"}
            for p in s.get("params", []):
                assert "default" in p and "label" in p
