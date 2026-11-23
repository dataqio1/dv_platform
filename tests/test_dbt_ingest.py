"""A failed dbt test opens one incident. The same test passing later resolves it."""

from __future__ import annotations

API = "/api/v1"

RUN = {
    "metadata": {"invocation_id": "inv-1", "dbt_version": "1.8.0"},
    "results": [
        {
            "unique_id": "test.analytics.not_null_fct_orders_order_id.16e066b9",
            "status": "fail",
            "message": "Got 3 results, configured to fail if != 0",
            "failures": 3,
            "relation_name": '"ANALYTICS"."dbt_test__audit"."not_null_fct_orders_order_id"',
        },
        {
            "unique_id": "test.analytics.unique_fct_customers_id.aaaa1111",
            "status": "skipped",
        },
        {
            "unique_id": "model.analytics.fct_orders",
            "status": "error",
            "message": "Database Error in model fct_orders",
            "relation_name": '"ANALYTICS"."MART"."FCT_ORDERS"',
        },
    ],
}


def test_failed_test_and_model_error_open_once_and_a_pass_resolves(client):
    first = client.post(f"{API}/dbt/runs", json=RUN).json()["data"]
    assert first["invocation_id"] == "inv-1"
    assert first["failed"] == 2
    assert first["skipped"] == 1
    assert first["opened"] == 2
    assert first["passed"] == 0

    incidents = client.get(f"{API}/incidents?active_only=true").json()["data"]
    by_kind = {i["kind"]: i for i in incidents}
    test_inc = by_kind["dbt_test"]
    assert "not_null_fct_orders_order_id" in test_inc["summary"]
    assert "Got 3 results" in test_inc["summary"]
    assert test_inc["severity"] == "warning"
    assert test_inc["monitor_id"] is None
    assert test_inc["evidence"]["failures"] == 3
    model = by_kind["dbt_run"]
    assert model["severity"] == "critical"
    assert model["table_ref"] == "ANALYTICS.MART.FCT_ORDERS"
    assert "fct_orders" in model["summary"]

    again = client.post(f"{API}/dbt/runs", json=RUN).json()["data"]
    assert again["opened"] == 0
    assert again["updated"] == 2
    still = client.get(f"{API}/incidents?active_only=true").json()["data"]
    assert len([i for i in still if i["kind"] in {"dbt_test", "dbt_run"}]) == 2
    assert next(i for i in still if i["kind"] == "dbt_test")["occurrences"] == 2

    cleared = client.post(
        f"{API}/dbt/runs",
        json={
            "metadata": {"invocation_id": "inv-2"},
            "results": [
                {
                    "unique_id": "test.analytics.not_null_fct_orders_order_id.16e066b9",
                    "status": "pass",
                }
            ],
        },
    ).json()["data"]
    assert cleared["passed"] == 1
    assert cleared["resolved"] == 1
    active = client.get(f"{API}/incidents?active_only=true").json()["data"]
    assert [i for i in active if i["kind"] == "dbt_test"] == []
    assert any(i["kind"] == "dbt_run" for i in active)


def test_a_run_without_results_is_rejected(client):
    res = client.post(f"{API}/dbt/runs", json={"metadata": {}})
    assert res.status_code == 422
