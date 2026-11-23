"""Sample-based DQ rule generation + dry-run, end to end over a REAL local SQL monitor (no mocks).
Generation profiles the sample and returns suggestions (nothing saved); dry-run tests specs on the
same sample without persisting."""

from __future__ import annotations

API = "/api/v1"

COORDS = {
    "database": "RAW", "schema": "PUBLIC",
    "tables": {
        "ORDERS": {
            "columns": [
                {"name": "ID", "data_type": "NUMBER"},
                {"name": "AMT", "data_type": "NUMBER"},
                {"name": "NOTE", "data_type": "VARCHAR"},
            ],
            "rows": [
                {"ID": 1, "AMT": 10, "NOTE": "a"},
                {"ID": 2, "AMT": 20, "NOTE": None},
                {"ID": 3, "AMT": 30, "NOTE": "c"},
            ],
        },
    },
}


def _monitor(client):
    cid = client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "c", "auth_method": "none", "coordinates": COORDS},
    ).json()["data"]["id"]
    return client.post(
        f"{API}/monitors",
        json={"connection_id": cid, "name": "orders", "scope": {"schema": "PUBLIC", "tables": ["ORDERS"]}},
    ).json()["data"]["id"]


def test_suggest_from_sample_generates_rules_and_saves_nothing(client):
    mid = _monitor(client)
    d = client.post(f"{API}/monitors/{mid}/rules:suggest-from-sample",
                    json={"level": 2, "sample_size": 1000}).json()["data"]

    assert d["sampled"] == 3 and d["level"] == 2
    names = {r["name"] for r in d["rules"]}
    assert "not_null__ID" in names        # ID is never null
    assert "not_null__AMT" in names
    assert "not_null__NOTE" not in names  # NOTE has a null → no not_null rule
    assert any(n.startswith("null_rate__NOTE") for n in names)  # partial-null → null_rate
    assert any(n.startswith("range__AMT") for n in names)       # numeric range
    assert all(r["state"] == "suggested" for r in d["rules"])

    # Nothing was persisted — the monitor still has no rules.
    rules = client.get(f"{API}/monitors/{mid}/rules").json()["data"]
    assert rules == []


def test_dry_run_rules_flags_violations_without_saving(client):
    mid = _monitor(client)
    specs = [
        {"name": "not_null__NOTE", "type": "not_null", "columns": ["NOTE"], "config": {}},
        {"name": "amt_under_25", "type": "range", "columns": ["AMT"], "config": {"min_value": 0, "max_value": 25}},
        {"name": "id_unique", "type": "unique", "columns": ["ID"], "config": {}},
    ]
    d = client.post(f"{API}/monitors/{mid}/rules:dry-run",
                    json={"rules": specs, "sample_size": 1000}).json()["data"]

    assert d["sampled"] == 3
    by = {r["name"]: r for r in d["results"]}
    assert by["not_null__NOTE"]["passed"] is False and by["not_null__NOTE"]["measured"] == 1
    assert by["amt_under_25"]["passed"] is False and by["amt_under_25"]["measured"] == 1  # the 30 row
    assert by["id_unique"]["passed"] is True
    # failing examples are returned for triage
    assert len(by["not_null__NOTE"]["failing"]) == 1

    # Still nothing persisted.
    assert client.get(f"{API}/monitors/{mid}/rules").json()["data"] == []
