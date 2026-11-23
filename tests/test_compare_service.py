"""Compare service: job lifecycle + cell-level results through the ResultStore (D9)."""

from __future__ import annotations

from dvp.testing_engine.service import CompareService

SRC = [{"ID": 1, "AMT": "10.00"}, {"ID": 2, "AMT": "20.00"}]
DST = [{"ID": 1, "AMT": "10.00"}, {"ID": 2, "AMT": "22.00"}]


def test_compare_job_run_and_drilldown(container, ctx, tmp_path):
    svc = CompareService(container.metadata, container.results)
    job = svc.create_job(
        ctx,
        kind="data",
        source={"table": "SRC"},
        target={"table": "DST"},
        options={},
    )
    assert job["status"] == "queued"

    result = svc.run_inmemory(
        ctx,
        job["id"],
        src_rows=SRC,
        dst_rows=DST,
        key=["ID"],
        columns=["AMT"],
        column_types={"AMT": "NUMBER"},
    )
    assert result["status"] == "succeeded"
    summary = result["result"]["summary"]
    assert summary["matched_rows"] == 1
    assert summary["mismatched_rows"] == 1
    assert summary["per_column_mismatch"] == {"AMT": 1}

    # Cell-level detail is retrievable via the ResultStore, bucketed.
    diffs = svc.read_differences(ctx, job["id"], bucket="value_difference")
    mismatch = diffs[0]
    assert mismatch["key"] == {"ID": 2}
    assert mismatch["cells"][0]["column"] == "AMT"


def test_update_job_edits_definition_partially(container, ctx):
    """Editing a test case changes only the fields sent; the rest (and its id) are preserved."""
    svc = CompareService(container.metadata, container.results)
    job = svc.create_job(
        ctx,
        kind="data",
        name="orig",
        source={"table": "SRC"},
        target={"table": "DST"},
        options={"key": ["ID"], "tolerance_pct": 0},
    )
    jid = job["id"]

    # Partial edit: rename + change the compare options; source/target left untouched.
    updated = svc.update_job(
        ctx, jid, {"name": "renamed", "options": {"key": ["ID", "REGION"], "tolerance_pct": 5}}
    )
    assert updated["id"] == jid
    assert updated["name"] == "renamed"
    assert updated["options"] == {"key": ["ID", "REGION"], "tolerance_pct": 5}
    assert updated["source"] == {"table": "SRC"}  # untouched
    assert updated["target"] == {"table": "DST"}  # untouched

    # A field absent from the payload is never changed (true partial update).
    again = svc.update_job(ctx, jid, {"target": {"table": "DST2"}})
    assert again["name"] == "renamed"  # still the earlier edit
    assert again["target"] == {"table": "DST2"}

    # The edit is persisted, not just returned.
    assert svc.get_job(ctx, jid)["target"] == {"table": "DST2"}


def test_batch_edit_attach_rename_and_remove_pairs(container, ctx):
    """Editing a batch test case: rename it (every pair's batch_name), edit a pair's definition,
    add a new pair to the batch, and remove an existing one."""
    svc = CompareService(container.metadata, container.results)
    bid = "batch-edit-xyz"
    j1 = svc.create_job(ctx, kind="data", name="b: A", source={"table": "A"},
                        target={"table": "A"}, options={"key": ["ID"]}, batch_id=bid, batch_name="b")
    j2 = svc.create_job(ctx, kind="data", name="b: B", source={"table": "B"},
                        target={"table": "B"}, options={}, batch_id=bid, batch_name="b")
    batch = svc.get_batch(ctx, bid)
    assert batch["total"] == 2 and batch["name"] == "b"

    # Rename the batch (apply to every pair) + edit one pair's options.
    svc.update_job(ctx, j1["id"], {"batch_name": "renamed", "options": {"key": ["ID", "REGION"]}})
    svc.update_job(ctx, j2["id"], {"batch_name": "renamed"})
    assert svc.get_batch(ctx, bid)["name"] == "renamed"

    # Add a pair, remove the original second pair.
    svc.create_job(ctx, kind="data", name="renamed: C", source={"table": "C"},
                   target={"table": "C"}, options={}, batch_id=bid, batch_name="renamed")
    svc.delete_job(ctx, j2["id"])
    batch = svc.get_batch(ctx, bid)
    assert sorted(j["source"]["table"] for j in batch["jobs"]) == ["A", "C"]
    edited = next(j for j in batch["jobs"] if j["source"]["table"] == "A")
    assert edited["options"]["key"] == ["ID", "REGION"]


def test_resolve_pairs_covers_names_specs_and_sql(container):
    """Batch pairs normalise three ways: plain table names, name-mapped pairs, and fully
    independent per-side specs (table/SQL/file) from the dataset-mapping UI."""
    svc = CompareService(container.metadata, container.results)
    src = {"connection_id": "C_SRC", "schema": "PUBLIC"}
    tgt = {"connection_id": "C_TGT", "schema": "DWH"}

    # 1) `tables` → same name both sides, merged onto batch connections.
    out = svc._resolve_pairs(["ORDERS"], None, src, tgt)
    assert out == [({"connection_id": "C_SRC", "schema": "PUBLIC", "table": "ORDERS"},
                    {"connection_id": "C_TGT", "schema": "DWH", "table": "ORDERS"}, "ORDERS", {})]

    # 2) name-mapped pair → differing names, label shows the arrow.
    out = svc._resolve_pairs(None, [{"source_table": "ORD", "target_table": "ORDERS"}], src, tgt)
    (s0, t0, label, _o) = out[0]
    assert s0["table"] == "ORD" and t0["table"] == "ORDERS" and label == "ORD→ORDERS"

    # 3) full independent specs — a source TABLE mapped to a target SQL (different connections),
    #    carrying a per-pair column mapping in its own options.
    out = svc._resolve_pairs(
        None,
        [{
            "source": {"connection_id": "C_MONGO", "table": "orders"},
            "target": {"connection_id": "C_MSSQL", "sql": "SELECT * FROM dbo.Orders"},
            "label": "orders → dbo.Orders (SQL)",
            "options": {"column_mappings": {"ID": "ORDER_ID"}},
        }],
        src, tgt,
    )
    (s1, t1, label, opts) = out[0]
    assert s1 == {"connection_id": "C_MONGO", "schema": "PUBLIC", "table": "orders"}
    # SQL side is clean: connection + sql only, no leftover schema/table from the batch default.
    assert t1 == {"connection_id": "C_MSSQL", "sql": "SELECT * FROM dbo.Orders"}
    assert label == "orders → dbo.Orders (SQL)"
    assert opts == {"column_mappings": {"ID": "ORDER_ID"}}


def test_created_by_and_run_by_are_recorded_and_resolved(container, ctx):
    """Attribution: the creator is stamped on create; the executor on run — both surfaced
    with resolved display names on the list/detail paths."""
    svc = CompareService(container.metadata, container.results)
    job = svc.create_job(ctx, kind="data", source={"table": "SRC"}, target={"table": "DST"})
    assert job["created_by"] == ctx.user_id
    assert job["last_run_by"] is None  # not run yet

    svc.run_inmemory(ctx, job["id"], src_rows=SRC, dst_rows=DST, key=["ID"], columns=["AMT"],
                     column_types={"AMT": "NUMBER"})

    # list view resolves ids → names (bootstrap seeds a "Local User").
    listed = next(j for j in svc.list_jobs(ctx) if j["id"] == job["id"])
    assert listed["created_by"] == ctx.user_id
    assert listed["last_run_by"] == ctx.user_id
    assert listed["created_by_name"] == "Local User"
    assert listed["last_run_by_name"] == "Local User"

    # detail view resolves too.
    got = svc.get_job(ctx, job["id"])
    assert got["created_by_name"] == "Local User" and got["last_run_by_name"] == "Local User"


def test_create_batch_is_queued_then_requeue_resets(container, ctx):
    """Async batch: create_batch leaves jobs 'queued' (worker runs them later); the batch
    summary reads 'running' while any pair is unfinished; requeue_batch re-queues them all."""
    from dvp.testing_engine.service import CompareService

    svc = CompareService(container.metadata, container.results)
    src = {"connection_id": "C"}
    pairs = [{"source": {"table": "A"}, "target": {"table": "A"}},
             {"source": {"table": "B"}, "target": {"table": "B"}}]
    batch_id, job_ids = svc.create_batch(ctx, kind="data", name="nightly", source=src, target=src, table_pairs=pairs)
    assert len(job_ids) == 2

    batch = svc.get_batch(ctx, batch_id)
    assert batch["status"] == "running"  # nothing has executed yet (all queued)
    assert all(j["status"] == "queued" for j in batch["jobs"])

    # Simulate one finishing, then rerun → everything back to queued.
    svc.mark_failed(ctx, job_ids[0], "boom")
    out = svc.requeue_batch(ctx, batch_id)
    assert {jid for jid, _ in out} == set(job_ids)
    again = svc.get_batch(ctx, batch_id)
    assert again["status"] == "running" and all(j["status"] == "queued" for j in again["jobs"])
    assert all(j["error"] is None for j in again["jobs"])  # prior failure cleared


def test_active_jobs_only_shows_live_runs(container, ctx):
    """The queue bell must show only work actually in flight. A created-but-never-run test case
    also carries status 'queued' in the model, so active_jobs intersects with the live-run set
    (ids the worker pool is handling). Without that set (subprocess mode) it falls back to status."""
    svc = CompareService(container.metadata, container.results)
    a = svc.create_job(ctx, kind="count", source={"table": "A"}, target={"table": "A"}, options={})
    b = svc.create_job(ctx, kind="count", source={"table": "B"}, target={"table": "B"}, options={})

    # Both are "queued" in the model but neither was submitted → nothing is live.
    svc.active_runs = set()
    assert svc.active_jobs(ctx)["total"] == 0

    # Submitting one adds it to the live set → only that one shows, as waiting.
    svc.active_runs = {a["id"]}
    out = svc.active_jobs(ctx)
    assert out["total"] == 1 and out["jobs"][0]["id"] == a["id"]
    assert out["queued"] == 1 and out["running"] == 0

    # No live set (subprocess mode) → fall back to status, so both "queued" jobs show.
    svc.active_runs = None
    assert svc.active_jobs(ctx)["total"] == 2
    assert {j["id"] for j in svc.active_jobs(ctx)["jobs"]} == {a["id"], b["id"]}


def test_diff_tolerance_allows_some_differences():
    """Data validation 'allowed differences': strict identity stays under `identical`, while
    `match` relaxes to within the user's row/percentage budget."""
    from dvp.testing_engine.service import _apply_diff_tolerance

    base = {"keys_total": 1000, "value_difference_rows": 5, "source_only_rows": 2,
            "target_only_rows": 0, "ambiguous_rows": 0,
            "match": False}  # 7 differing rows of 1000 → not identical

    # No budget → strict fail, but identity + diff stats are recorded.
    s = dict(base); _apply_diff_tolerance(s, {})
    assert s["identical"] is False and s["match"] is False
    assert s["total_diff_rows"] == 7 and s["diff_pct"] == 0.7

    # Absolute budget of 10 rows → passes.
    s = dict(base); _apply_diff_tolerance(s, {"max_diff_rows": 10})
    assert s["match"] is True and s["identical"] is False

    # 0.5% budget → 0.7% exceeds it → fails.
    s = dict(base); _apply_diff_tolerance(s, {"max_diff_pct": 0.5})
    assert s["match"] is False

    # 1% budget → 0.7% within → passes.
    s = dict(base); _apply_diff_tolerance(s, {"max_diff_pct": 1})
    assert s["match"] is True


def test_counts_direction_and_bounded_tolerance():
    """Counts pass-condition: direction (sign) + bounded tolerance (% OR rows). Covers
    'source may be higher by at most 1% or 1000 rows'."""
    from dvp.connectors.base import TableRef
    from dvp.connectors.inline import InlineConnector

    def run(src_n, dst_n, options):
        svc = CompareService.__new__(CompareService)  # pure number-crunching, no store needed
        counts = {"S": src_n, "T": dst_n}

        def side(_ctx, spec):
            t = spec["table"]
            conn = InlineConnector({"schema": "P", "tables": {t: {"columns": [{"name": "ID"}], "row_count": counts[t]}}})
            return conn, t, TableRef(database="", schema="P", table=t)
        svc._side = side  # type: ignore[assignment]
        # _count_from now uses the connector's native count() for a table side.
        s, *_ = svc._run_count(None, {"table": "S"}, {"table": "T"}, options)
        return s["match"]

    opt = {"direction": "source_higher", "tolerance_pct": 1, "tolerance_rows": 1000}
    assert run(100000, 100000, opt) is True       # equal → ok
    assert run(100500, 100000, opt) is True        # +0.5% (and <1000) → ok
    assert run(101000, 100000, opt) is True        # +1000 rows exactly → ok (row budget)
    assert run(100900, 100000, opt) is True        # +0.9% → within % budget
    assert run(103000, 100000, opt) is False       # +3% and +3000 rows → exceeds both → fail
    assert run(99000, 100000, opt) is False         # source LOWER → wrong direction → fail

    # Exact (default) and symmetric tolerance.
    assert run(100000, 100000, {}) is True
    assert run(100001, 100000, {}) is False                                  # exact → any gap fails
    assert run(100500, 100000, {"direction": "both", "tolerance_pct": 1}) is True   # within ±1%


def test_primary_key_from_inline_source_feeds_key_detection():
    """A declared PK is reported by the connector and preferred as the compare key."""
    from dvp.connectors.base import TableRef
    from dvp.connectors.inline import InlineConnector

    conn = InlineConnector({
        "schema": "PUBLIC",
        "tables": {"ORDERS": {
            "columns": [{"name": "ID"}, {"name": "REGION"}, {"name": "AMT"}],
            "primary_key": ["ID"],
            "rows": [{"ID": 1, "REGION": "EU", "AMT": 10}],
        }},
    })
    pk = conn.primary_key(TableRef(database="", schema="PUBLIC", table="ORDERS"))
    assert pk == ["ID"]
    assert CompareService._primary_key(conn, TableRef(database="", schema="PUBLIC", table="ORDERS")) == ["ID"]


def test_result_detail_stays_out_of_metadata_db(container, ctx):
    """RES-01: real values live in the ResultStore, not the metadata DB."""
    svc = CompareService(container.metadata, container.results)
    job = svc.create_job(ctx, kind="data", source={}, target={}, options={})
    svc.run_inmemory(
        ctx,
        job["id"],
        src_rows=SRC,
        dst_rows=DST,
        key=["ID"],
        columns=["AMT"],
        column_types={"AMT": "NUMBER"},
    )
    got = svc.get_job(ctx, job["id"])
    # The DB row holds only a pointer + aggregates, not the differing values.
    assert got["result"]["detail_ref"].startswith("file://")
    assert "22.00" not in str(got["result"]["summary"])


def test_delete_job_cascades_to_result_and_artifact(container, ctx):
    """Deleting a test case removes its result row AND the detail artifact (no orphans)."""
    import os

    from dvp.core.errors import NotFoundError

    svc = CompareService(container.metadata, container.results)
    job = svc.create_job(ctx, kind="data", source={"table": "SRC"}, target={"table": "DST"})
    svc.run_inmemory(
        ctx, job["id"], src_rows=SRC, dst_rows=DST, key=["ID"], columns=["AMT"],
        column_types={"AMT": "NUMBER"},
    )
    detail = svc.get_job(ctx, job["id"])["result"]["detail_ref"]
    path = detail.removeprefix("file://")
    assert os.path.exists(path)

    out = svc.delete_job(ctx, job["id"])
    assert out["artifacts_deleted"] == 1 and out["warnings"] == []
    assert not os.path.exists(path)  # artifact physically gone
    try:
        svc.get_job(ctx, job["id"])
        raise AssertionError("job should be gone")
    except NotFoundError:
        pass


def test_delete_batch_removes_all_jobs_and_artifacts(container, ctx):
    import os

    svc = CompareService(container.metadata, container.results)
    paths = []
    bid = None
    for name in ("A", "B", "C"):
        job = svc.create_job(ctx, kind="data", source={"table": name}, target={"table": name})
        # stamp them into one batch via the model (create_job has no batch arg)
        with container.metadata.session(ctx) as s:
            from dvp.db.models import CompareJob
            j = s.get(CompareJob, job["id"])
            j.batch_id = bid = bid or "batch-xyz"
        svc.run_inmemory(ctx, job["id"], src_rows=SRC, dst_rows=DST, key=["ID"],
                         columns=["AMT"], column_types={"AMT": "NUMBER"})
        paths.append(svc.get_job(ctx, job["id"])["result"]["detail_ref"].removeprefix("file://"))

    out = svc.delete_batch(ctx, bid)
    assert out["deleted_jobs"] == 3 and out["artifacts_deleted"] == 3
    assert all(not os.path.exists(p) for p in paths)


def test_delete_job_is_tenant_scoped(container, ctx, other_group_ctx):
    """A group cannot delete another group's test case."""
    from dvp.core.errors import NotFoundError

    svc = CompareService(container.metadata, container.results)
    job = svc.create_job(ctx, kind="data", source={"table": "SRC"}, target={"table": "DST"})
    svc.run_inmemory(ctx, job["id"], src_rows=SRC, dst_rows=DST, key=["ID"],
                     columns=["AMT"], column_types={"AMT": "NUMBER"})
    try:
        svc.delete_job(other_group_ctx, job["id"])
        raise AssertionError("cross-group delete must be rejected")
    except NotFoundError:
        pass
    # original owner can still see it (it was not deleted)
    assert svc.get_job(ctx, job["id"])["id"] == job["id"]


def test_cancel_job_marks_cancelled(container, ctx):
    """Cancelling a queued/running compare flags it in the shared registry and sets status."""
    from dvp.testing_engine.cancel import CancelRegistry

    reg = CancelRegistry()
    svc = CompareService(container.metadata, container.results, cancel_registry=reg)
    job = svc.create_job(ctx, kind="data", source={"table": "SRC"}, target={"table": "DST"})
    out = svc.cancel_job(ctx, job["id"])
    assert out["status"] == "cancelled"
    assert reg.is_cancelled(job["id"])


def test_execution_log_is_served_and_deleted_with_the_job(container, ctx):
    """Logs are stored on the job (so they exist for any run) and vanish when it's deleted."""
    import pytest

    from dvp.core.errors import NotFoundError
    from dvp.db.models import CompareJob

    svc = CompareService(container.metadata, container.results)
    job = svc.create_job(ctx, kind="data", source={"table": "S"}, target={"table": "T"})
    assert svc.get_logs(ctx, job["id"]) == []  # nothing run yet

    with container.metadata.session(ctx) as s:
        s.get(CompareJob, job["id"]).execution_log = [
            {"ts": "2026-10-25T00:00:00", "elapsed_ms": 0, "level": "info", "msg": "Execution started"},
            {"ts": "2026-10-25T00:00:01", "elapsed_ms": 900, "level": "info", "msg": "Result: MATCH"},
        ]
    logs = svc.get_logs(ctx, job["id"])
    assert [ln["msg"] for ln in logs] == ["Execution started", "Result: MATCH"]
    assert next(j for j in svc.list_jobs(ctx) if j["id"] == job["id"])["has_log"] is True

    svc.delete_job(ctx, job["id"])  # job gone → inline log gone with it
    with pytest.raises(NotFoundError):
        svc.get_logs(ctx, job["id"])
