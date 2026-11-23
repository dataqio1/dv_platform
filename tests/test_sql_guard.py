"""The compare-engine SQL guard: filters must be a single boolean predicate and raw-SQL sides a
single read-only SELECT/WITH. Pure-unit coverage of the validator plus an end-to-end check that a
payload smuggled through a {{variable}} is rejected at run time (real service, no mocks)."""

from __future__ import annotations

import pytest

from dvp.core.errors import ValidationError
from dvp.testing_engine.service import CompareService
from dvp.testing_engine.sql_guard import validate_filter, validate_subquery_sql

# --------------------------------------------------------------------------- filters: allowed

GOOD_FILTERS = [
    "AMOUNT > 0",
    "status IN ('A', 'B') AND region = 'US'",
    "DT = '2026-10-01'",
    "name LIKE '%union%'",  # 'union' inside a literal is fine (literals are scrubbed)
    "note = 'a;b'",  # ';' inside a literal is fine
    "updated_at > '2024-01-01'",  # 'update' as a substring of a column name is fine
]


@pytest.mark.parametrize("pred", GOOD_FILTERS)
def test_valid_filters_pass(pred):
    assert validate_filter(pred) == pred


@pytest.mark.parametrize("pred", [None, "", "   "])
def test_empty_filter_is_fine(pred):
    assert validate_filter(pred) == pred


# --------------------------------------------------------------------------- filters: rejected

BAD_FILTERS = [
    "1=1; DROP TABLE orders",  # stacked statement
    "1=1 UNION SELECT secret FROM creds",  # union piggyback
    "1=1 -- comment out the rest",  # line comment
    "1=1 /* block */",  # block comment
    "status = 'A' INTERSECT SELECT 1",  # set operator
    "amount > (DELETE FROM orders)",  # DML keyword
]


@pytest.mark.parametrize("pred", BAD_FILTERS)
def test_malicious_filters_rejected(pred):
    with pytest.raises(ValidationError):
        validate_filter(pred)


# --------------------------------------------------------------------------- raw sql sides

GOOD_SQL = [
    "SELECT ID, AMT FROM ORDERS",
    "SELECT ID, AMT FROM ORDERS;",  # a single trailing ';' is tolerated
    'SELECT "ORDER_ID","AMOUNT" FROM "ORDERS" WHERE "ORDER_ID" <= 2',
    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    "SELECT * FROM ORDERS WHERE note = 'has a ; and union inside'",
]


@pytest.mark.parametrize("sql", GOOD_SQL)
def test_valid_sql_passes(sql):
    assert validate_subquery_sql(sql) == sql


BAD_SQL = [
    "SELECT 1; DROP TABLE orders",  # stacked statement
    "DROP TABLE orders",  # not a SELECT
    "DELETE FROM orders",  # not read-only
    "SELECT 1 -- trailing comment",  # comment
    "INSERT INTO orders VALUES (1)",  # not a SELECT
]


@pytest.mark.parametrize("sql", BAD_SQL)
def test_malicious_sql_rejected(sql):
    with pytest.raises(ValidationError):
        validate_subquery_sql(sql)


# --------------------------------------------------------------------------- end-to-end


def test_create_job_rejects_malicious_filter(container, ctx):
    svc = CompareService(container.metadata, container.results)
    with pytest.raises(ValidationError):
        svc.create_job(
            ctx,
            kind="data",
            source={"table": "SRC", "filter": "1=1; DROP TABLE orders"},
            target={"table": "DST"},
            options={},
        )


def test_run_rejects_payload_smuggled_through_a_variable(container, ctx):
    """The stored filter looks benign ({{f}}); the malicious value arrives at run time and must be
    caught after substitution — before it reaches a connector."""
    svc = CompareService(container.metadata, container.results)
    job = svc.create_job(
        ctx,
        kind="count",
        source={"table": "SRC", "filter": "{{f}}"},
        target={"table": "DST"},
        options={},
    )
    with pytest.raises(ValidationError):
        svc.run(ctx, job["id"], variables={"f": "1=1 UNION SELECT token FROM secrets"})

    # And the job is resolved to 'failed', never left stuck 'running'.
    assert svc.get_job(ctx, job["id"])["status"] == "failed"
