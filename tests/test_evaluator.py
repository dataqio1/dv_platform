"""Pushdown rule evaluator + monitor runner over the real local SQLite connector."""

from __future__ import annotations

from datetime import UTC, datetime

from dvp.connectors.base import TableRef
from dvp.connectors.local_sql import LocalSqlConnector
from dvp.observability.evaluator import build_sql, evaluate

API = "/api/v1"

COORDS = {
    "database": "RAW_SALES",
    "schema": "PUBLIC",
    "tables": {
        "RAW_ORDERS": {
            "columns": [
                {"name": "ORDER_ID", "data_type": "NUMBER", "nullable": False},
                {"name": "AMOUNT", "data_type": "NUMBER", "nullable": False},
                {"name": "EMAIL", "data_type": "VARCHAR", "nullable": True},
                {"name": "INGESTED_AT", "data_type": "TIMESTAMP_NTZ", "nullable": False},
            ],
            "rows": [
                {
                    "ORDER_ID": 1,
                    "AMOUNT": 10,
                    "EMAIL": "a@x.com",
                    "INGESTED_AT": "2026-10-01T00:00:00",
                },
                {"ORDER_ID": 2, "AMOUNT": -5, "EMAIL": None, "INGESTED_AT": "2026-10-02T00:00:00"},
                {
                    "ORDER_ID": 3,
                    "AMOUNT": 20,
                    "EMAIL": "c@x.com",
                    "INGESTED_AT": "2026-10-03T00:00:00",
                },
            ],
        }
    },
}
TABLE = "RAW_SALES.PUBLIC.RAW_ORDERS"


# ----------------------------- pure / connector -----------------------------
def _conn() -> LocalSqlConnector:
    return LocalSqlConnector(COORDS)


def _tref() -> TableRef:
    return TableRef("RAW_SALES", "PUBLIC", "RAW_ORDERS")


def test_local_sql_connector_runs_real_sql():
    c = _conn()
    assert c.count(_tref()) == 3
    assert c.latest_watermark(_tref(), "INGESTED_AT") == "2026-10-03T00:00:00"
    res = c.execute('SELECT COUNT(*) FROM "RAW_ORDERS" WHERE "AMOUNT" < 0')
    assert res.rows[0][0] == 1


def test_evaluate_custom_sql_fails_on_negative():
    rule = {
        "type": "custom_sql",
        "columns": [],
        "config": {
            "expression": 'SELECT COUNT(*) AS v FROM {{table}} WHERE "AMOUNT" < 0',
            "threshold": 0,
        },
    }
    ev = evaluate(_conn(), _tref(), rule)
    assert ev.metric_value == 1
    assert ev.passed is False
    assert ev.observed["failing_keys"]
    assert any(row.get("AMOUNT") == -5 for row in ev.observed["failing_keys"])
    assert "LIMIT 20" in ev.observed["sample_sql"]


def test_evaluate_not_null_and_range_and_rowcount():
    nn = evaluate(_conn(), _tref(), {"type": "not_null", "columns": ["EMAIL"], "config": {}})
    assert nn.metric_value == 1 and nn.passed is False  # one null email
    assert nn.observed["failing_keys"] == [{"EMAIL": None}]
    ok = evaluate(_conn(), _tref(), {"type": "row_count", "columns": [], "config": {"min_rows": 1}})
    assert ok.passed is True
    assert "failing_keys" not in ok.observed
    rng = evaluate(
        _conn(), _tref(), {"type": "range", "columns": ["AMOUNT"], "config": {"min_value": 0}}
    )
    assert rng.metric_value == 1 and rng.passed is False  # the -5
    rc = evaluate(_conn(), _tref(), {"type": "row_count", "columns": [], "config": {"min_rows": 1}})
    assert rc.metric_value == 3 and rc.passed is True


def test_evaluate_freshness_lag():
    now = datetime(2026, 10, 4, 0, 0, 0, tzinfo=UTC)  # 1 day after last load
    rule = {"type": "freshness", "columns": ["INGESTED_AT"], "config": {"max_age_hours": 24}}
    ev = evaluate(_conn(), _tref(), rule, now=now)
    assert ev.metric_value == 86400  # 24h in seconds
    assert ev.passed is True
    ev2 = evaluate(_conn(), _tref(), rule, now=datetime(2026, 10, 6, tzinfo=UTC))
    assert ev2.passed is False  # 3 days > 24h


def test_build_sql_regex_dialects():
    rule = {"type": "regex", "columns": ["EMAIL"], "config": {"pattern": "@"}}
    assert "REGEXP_LIKE" in build_sql(rule, '"T"', dialect="snowflake")
    assert "REGEXP" in build_sql(rule, '"T"', dialect="sqlite")


# ----------------------------- end-to-end API -----------------------------
def _setup(client):
    conn = client.post(
        f"{API}/connections",
        json={
            "source_type": "local",
            "name": "orders",
            "auth_method": "none",
            "coordinates": COORDS,
        },
    ).json()["data"]
    mon = client.post(
        f"{API}/monitors",
        json={
            "connection_id": conn["id"],
            "name": "orders",
            "scope": {"database": "RAW_SALES", "schema": "PUBLIC", "tables": ["RAW_ORDERS"]},
            "watermark_column": "INGESTED_AT",
            "watermark_type": "timestamp",
        },
    ).json()["data"]
    return mon["id"]


def test_evaluate_rule_endpoint(client):
    mid = _setup(client)
    rid = client.post(
        f"{API}/monitors/{mid}/rules",
        json={
            "name": "no_negative",
            "type": "custom_sql",
            "table_ref": TABLE,
            "config": {
                "expression": 'SELECT COUNT(*) AS v FROM {{table}} WHERE "AMOUNT" < 0',
                "threshold": 0,
            },
        },
    ).json()["data"]["id"]
    out = client.post(f"{API}/monitors/{mid}/rules/{rid}:evaluate").json()["data"]
    assert out["metric_value"] == 1
    assert out["passed"] is False
    assert any(row.get("AMOUNT") == -5 for row in out["failing_keys"])
    assert "LIMIT 20" in out["sample_sql"]
    incidents = client.get(f"{API}/monitors/{mid}/incidents?active_only=true").json()["data"]
    failed = [i for i in incidents if i.get("dq_rule_id") == rid or "no_negative" in (i.get("summary") or "")]
    assert failed
    assert any(row.get("AMOUNT") == -5 for row in failed[0]["evidence"]["failing_keys"])
    # recorded → shows in history
    hist = client.get(f"{API}/monitors/{mid}/rules/{rid}/history").json()["data"]
    assert hist["points"][-1]["value"] == 1


def test_run_monitor_profiles_and_evaluates(client):
    mid = _setup(client)
    # two rules: one failing (not_null EMAIL), one passing (row_count)
    client.post(
        f"{API}/monitors/{mid}/rules",
        json={
            "name": "email_not_null",
            "type": "not_null",
            "columns": ["EMAIL"],
            "table_ref": TABLE,
            "severity": "critical",
        },
    )
    client.post(
        f"{API}/monitors/{mid}/rules",
        json={
            "name": "has_rows",
            "type": "row_count",
            "table_ref": TABLE,
            "config": {"min_rows": 1},
        },
    )

    summary = client.post(f"{API}/monitors/{mid}:run").json()["data"]
    assert summary["rules_evaluated"] == 2
    assert summary["tables"][0]["table_ref"] == TABLE
    assert summary["query_shape"] == "check"
    assert "count_star_and_null_rates" not in summary["tables"][0]["queries"]

    # a check measures volume and schema but does not score quality yet. The quality pillar is
    # still "configured" (it's in play) — the UI shows WHY there's no score, not a setup prompt.
    cards = client.get(f"{API}/monitors/{mid}/tables/{TABLE}/pillars").json()["data"]
    by = {c["pillar"]: c for c in cards["pillars"]}
    assert by["volume"]["headline"] == "3 rows"
    assert by["quality"]["configured"] is True
    assert by["quality"]["status"] == "unknown"
    assert by["quality"]["headline"] == "The last check did not score columns"
    assert cards["query_shape"] == "check"
    assert by["schema"]["headline"] == "4 columns"

    profiled = client.post(f"{API}/monitors/{mid}/tables/{TABLE}:profile").json()["data"]
    assert profiled["query_shape"] == "full_profile"
    cards = client.get(f"{API}/monitors/{mid}/tables/{TABLE}/pillars").json()["data"]
    by = {c["pillar"]: c for c in cards["pillars"]}
    assert by["quality"]["configured"] is True

    # the failing rule shows in the table panel with a fail
    rules = client.get(f"{API}/monitors/{mid}/tables/{TABLE}/rules").json()["data"]
    email_rule = next(r for r in rules if r["name"] == "email_not_null")
    assert email_rule["latest"]["passed"] is False
    assert email_rule["latest"]["metric_value"] == 1
