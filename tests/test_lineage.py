"""A table page names models more than one hop away."""

from __future__ import annotations

API = "/api/v1"


def _monitor(client) -> tuple[str, str]:
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "inline", "name": "c", "auth_method": "none", "coordinates": {}},
    ).json()["data"]["id"]
    mid = client.post(
        f"{API}/monitors",
        json={
            "connection_id": conn,
            "name": "orders",
            "scope": {"database": "RAW", "schema": "PUBLIC", "tables": ["ORDERS"]},
        },
    ).json()["data"]["id"]
    return mid, "RAW.PUBLIC.ORDERS"


def test_downstream_walks_past_one_hop(client):
    mid, table = _monitor(client)
    added = client.post(
        f"{API}/lineage/edges",
        json={
            "edges": [
                {"upstream": "RAW.PUBLIC.ORDERS", "downstream": "ANALYTICS.STG_ORDERS"},
                {"upstream": "ANALYTICS.STG_ORDERS", "downstream": "ANALYTICS.FCT_ORDERS"},
                {"upstream": "ANALYTICS.FCT_ORDERS", "downstream": "ANALYTICS.MART_REVENUE"},
            ]
        },
    ).json()["data"]
    assert added["added"] == 3

    again = client.post(
        f"{API}/lineage/edges",
        json={"edges": [{"upstream": "RAW.PUBLIC.ORDERS", "downstream": "ANALYTICS.STG_ORDERS"}]},
    ).json()["data"]
    assert again["added"] == 0

    cards = client.get(f"{API}/monitors/{mid}/tables/{table}/pillars").json()["data"]
    down = cards["lineage"]["downstream"]
    assert [(n["name"], n["hops"]) for n in down] == [
        ("ANALYTICS.STG_ORDERS", 1),
        ("ANALYTICS.FCT_ORDERS", 2),
        ("ANALYTICS.MART_REVENUE", 3),
    ]
    assert cards["lineage"]["status"] == "mapped"

    gold = client.get(
        f"{API}/monitors/{mid}/tables/ANALYTICS.MART_REVENUE/pillars"
    ).json()["data"]
    assert [(n["name"], n["hops"]) for n in gold["lineage"]["upstream"]] == [
        ("ANALYTICS.FCT_ORDERS", 1),
        ("ANALYTICS.STG_ORDERS", 2),
        ("RAW.PUBLIC.ORDERS", 3),
    ]


def test_a_cycle_stops(client):
    mid, table = _monitor(client)
    client.post(
        f"{API}/lineage/edges",
        json={
            "edges": [
                {"upstream": table, "downstream": "ANALYTICS.STG_ORDERS"},
                {"upstream": "ANALYTICS.STG_ORDERS", "downstream": table},
            ]
        },
    )
    cards = client.get(f"{API}/monitors/{mid}/tables/{table}/pillars").json()["data"]
    names = [n["name"] for n in cards["lineage"]["downstream"]]
    assert names == ["ANALYTICS.STG_ORDERS"]
