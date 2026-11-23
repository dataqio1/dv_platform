"""Runtime variables: {{var}} substitution at execution + recorded on the result."""

from __future__ import annotations

from dvp.testing_engine.variables import extract_variables, missing_variables, substitute

API = "/api/v1"

COORDS = {
    "database": "DB",
    "schema": "PUBLIC",
    "tables": {
        "PROD_ORDERS": {
            "columns": [{"name": "ID", "data_type": "NUMBER"}],
            "rows": [{"ID": 1}, {"ID": 2}],
        },
        "STG_ORDERS": {
            "columns": [{"name": "ID", "data_type": "NUMBER"}],
            "rows": [{"ID": 1}, {"ID": 2}, {"ID": 3}],
        },
    },
}


# ----------------------------- pure -----------------------------
def test_extract_and_substitute():
    defn = {"table": "{{env}}_ORDERS", "filter": "DT = '{{run_date}}'", "nested": ["{{env}}"]}
    assert extract_variables(defn) == {"env", "run_date"}
    out = substitute(defn, {"env": "PROD", "run_date": "2026-10-01"})
    assert out["table"] == "PROD_ORDERS"
    assert out["filter"] == "DT = '2026-10-01'"
    assert out["nested"] == ["PROD"]


def test_missing_variables():
    assert missing_variables({"t": "{{a}}_{{b}}"}, {"a": "x"}) == ["b"]


def test_substitute_database_schema_sql_in_side_spec():
    """A compare side may parameterize database, schema, table, or SQL — every string in
    the spec is substituted (the fields the wizard now exposes)."""
    source = {
        "connection_id": "c1",
        "database": "{{db}}",
        "schema": "{{env}}_RAW",
        "table": "{{tbl}}",
    }
    target = {"connection_id": "c1", "sql": "SELECT * FROM {{env}}_RAW.{{tbl}}"}
    assert extract_variables([source, target]) == {"db", "env", "tbl"}
    vals = {"db": "ANALYTICS", "env": "PROD", "tbl": "ORDERS"}
    s_out, t_out = substitute([source, target], vals)
    assert s_out["database"] == "ANALYTICS"
    assert s_out["schema"] == "PROD_RAW"
    assert s_out["table"] == "ORDERS"
    assert t_out["sql"] == "SELECT * FROM PROD_RAW.ORDERS"


# ----------------------------- end-to-end -----------------------------
def _conn(client):
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "c", "auth_method": "none", "coordinates": COORDS},
    ).json()["data"]["id"]


def test_run_with_variables_records_them(client):
    cid = _conn(client)
    # a count test case parameterised by {{env}}
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "count",
            "name": "env counts",
            "source": {"connection_id": cid, "table": "{{env}}_ORDERS"},
            "target": {"connection_id": cid, "table": "STG_ORDERS"},
        },
    ).json()["data"]

    # discoverable before running
    vars_needed = client.get(f"{API}/compares/{job['id']}/variables").json()["data"]
    assert vars_needed == ["env"]

    # run with env=PROD → PROD_ORDERS (2 rows) vs STG_ORDERS (3 rows)
    res = client.post(
        f"{API}/compares/{job['id']}:run", json={"variables": {"env": "PROD"}}
    ).json()["data"]
    assert res["status"] == "succeeded"
    assert res["result"]["summary"]["src_count"] == 2
    assert res["result"]["summary"]["dst_count"] == 3
    # the resolved variables are recorded on the execution
    assert res["result"]["variables"] == {"env": "PROD"}


def test_run_missing_variable_is_rejected(client):
    cid = _conn(client)
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "count",
            "name": "p",
            "source": {"connection_id": cid, "table": "{{env}}_ORDERS"},
            "target": {"connection_id": cid, "table": "STG_ORDERS"},
        },
    ).json()["data"]
    r = client.post(f"{API}/compares/{job['id']}:run", json={"variables": {}})
    assert r.status_code == 422
    assert "env" in r.json()["detail"]


def test_run_without_body_still_works_for_unparameterised(client):
    cid = _conn(client)
    job = client.post(
        f"{API}/compares",
        json={
            "kind": "count",
            "name": "plain",
            "source": {"connection_id": cid, "table": "PROD_ORDERS"},
            "target": {"connection_id": cid, "table": "STG_ORDERS"},
        },
    ).json()["data"]
    res = client.post(f"{API}/compares/{job['id']}:run").json()["data"]
    assert res["status"] == "succeeded"
    assert res["result"]["variables"] == {}
