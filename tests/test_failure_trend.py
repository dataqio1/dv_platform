"""Failure-growth-over-time for Data Quality test cases (the +/- trend), over REAL compare runs and
the real metadata DB (no mocks). Replicates the customer example: a run fails 100 records, the next
run with the SAME parameters fails 90 → the trend shows -10% (a decrease), scoped to that parameter
set only. Pure delta math is covered too.
"""

from __future__ import annotations

from dvp.testing_engine.service import (
    CompareService,
    failed_count_from_summary,
    failure_trend,
    param_hash,
)

# 100 source rows; targets differ by a controlled number of value mismatches.
SRC = [{"ID": i, "AMT": "10.00"} for i in range(1, 101)]
DST_100 = [{"ID": i, "AMT": "99.00"} for i in range(1, 101)]                       # 100 mismatches
DST_90 = [{"ID": i, "AMT": ("99.00" if i <= 90 else "10.00")} for i in range(1, 101)]  # 90 mismatches


def _run(svc, ctx, job_id, dst, variables):
    return svc.run_inmemory(
        ctx, job_id, src_rows=SRC, dst_rows=dst, key=["ID"], columns=["AMT"],
        column_types={"AMT": "NUMBER"}, variables=variables,
    )


# ----------------------------- pure math -----------------------------
def test_pct_change_and_direction():
    t = failure_trend([
        {"ts": "t1", "failed_count": 100, "status": "succeeded"},
        {"ts": "t2", "failed_count": 90, "status": "succeeded"},
    ])
    assert t["current"] == 90 and t["previous"] == 100
    assert t["abs_delta"] == -10 and t["pct_change"] == -10.0 and t["direction"] == "down"


def test_param_hash_is_order_independent_and_distinguishes_sets():
    assert param_hash({"a": 1, "b": 2}) == param_hash({"b": 2, "a": 1})
    assert param_hash({"region": "US"}) != param_hash({"region": "EU"})


def test_failed_count_normalizes_per_kind():
    assert failed_count_from_summary("data", {"value_difference_rows": 90}) == 90
    assert failed_count_from_summary("count", {"diff": -7}) == 7
    assert failed_count_from_summary("schema", {"source_only": 1, "type_mismatches": 2}) == 3


# ----------------------------- real runs → trend -----------------------------
def test_trend_shows_decrease_for_same_parameter_set(container, ctx):
    svc = CompareService(container.metadata, container.results)
    job = svc.create_job(ctx, kind="data", source={"table": "SRC"}, target={"table": "DST"}, options={})

    _run(svc, ctx, job["id"], DST_100, {"region": "US"})   # 100 failed
    _run(svc, ctx, job["id"], DST_90, {"region": "US"})    # 90 failed, same params

    trend = svc.failure_trend(ctx, job["id"])
    assert trend["current"] == 90
    assert trend["previous"] == 100
    assert trend["abs_delta"] == -10
    assert trend["pct_change"] == -10.0
    assert trend["direction"] == "down"
    assert len(trend["points"]) == 2  # two runs of the same parameter set


def test_trend_is_scoped_to_one_parameter_set(container, ctx):
    svc = CompareService(container.metadata, container.results)
    job = svc.create_job(ctx, kind="data", source={"table": "SRC"}, target={"table": "DST"}, options={})

    _run(svc, ctx, job["id"], DST_100, {"region": "US"})   # US set: 100
    _run(svc, ctx, job["id"], DST_90, {"region": "US"})    # US set: 90
    _run(svc, ctx, job["id"], DST_100, {"region": "EU"})   # EU set: 100 (different params)

    # Default = the latest run's parameter set (EU) — the US runs are NOT mixed in.
    latest = svc.failure_trend(ctx, job["id"])
    assert latest["current"] == 100 and latest["previous"] is None
    assert len(latest["points"]) == 1

    # Explicitly ask for the US set → its two runs and the -10% trend.
    us_hash = param_hash({"region": "US"})
    us = svc.failure_trend(ctx, job["id"], param_hash=us_hash)
    assert [p["failed_count"] for p in us["points"]] == [100, 90]
    assert us["pct_change"] == -10.0
