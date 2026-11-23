"""Progressive bulk load-type detection: a background run classifies every table in a monitor's
scope, streaming per-table results + progress (so 100+ tables fill a results table instead of
hanging one request). Real local connector + metadata DB, no mocks.
"""

from __future__ import annotations

from dvp.observability.service import MonitorService
from dvp.services.connections import ConnectionService
from dvp.services.load_type_detector import LoadTypeDetectorService

COORDS = {
    "database": "DW",
    "schema": "SRC",
    "tables": {
        "CUSTOMER_DIM": {"columns": [
            {"name": "CUSTOMER_ID", "data_type": "NUMBER"},
            {"name": "SRC_EFF_START_DATE", "data_type": "DATE"},
            {"name": "SRC_EFF_END_DATE", "data_type": "DATE"},
        ]},
        "EVENTS": {"columns": [
            {"name": "EVENT_ID", "data_type": "NUMBER"},
            {"name": "LOAD_TS", "data_type": "TIMESTAMP"},
        ]},
        "INVENTORY": {"columns": [
            {"name": "SKU", "data_type": "VARCHAR"},
            {"name": "QTY", "data_type": "NUMBER"},
        ]},
    },
}


def _setup(container, ctx) -> tuple[LoadTypeDetectorService, str]:
    cid = ConnectionService(container.metadata, container.secrets).create(
        ctx, source_type="local", name="dw", coordinates=COORDS, auth_method="none",
    )["id"]
    mon = MonitorService(container.metadata).create(
        ctx, connection_id=cid, name="bulk",
        scope={"database": "DW", "schema": "SRC", "tables": ["CUSTOMER_DIM", "EVENTS", "INVENTORY"]},
    )
    svc = LoadTypeDetectorService(container.metadata, container.secrets)
    return svc, mon["id"]


def test_start_creates_running_run_with_total(container, ctx):
    svc, mid = _setup(container, ctx)
    run = svc.start_for_monitor(ctx, mid)
    assert run["status"] == "running"
    assert run["progress"]["total"] == 3 and run["progress"]["processed"] == 0
    assert run["results"] == []


def test_run_detection_streams_results_and_completes(container, ctx):
    svc, mid = _setup(container, ctx)
    run = svc.start_for_monitor(ctx, mid)
    svc.run_detection(ctx, run["id"])

    done = svc.get(ctx, run["id"])
    assert done["status"] == "done"
    assert done["progress"] == {"pct": 100, "processed": 3, "total": 3}
    assert len(done["results"]) == 3

    by_table = {r["table_ref"]: r for r in done["results"]}
    # Every table in scope was classified (batched one-query schema fetch covered them all).
    assert set(by_table) == {"CUSTOMER_DIM", "EVENTS", "INVENTORY"}
    assert by_table["CUSTOMER_DIM"]["load_pattern"] == "scd2"
    assert by_table["EVENTS"]["load_pattern"] == "append"
    assert by_table["INVENTORY"]["load_pattern"] in ("full_snapshot", "reference")


def test_detection_works_connection_scoped_before_a_monitor_exists(container, ctx):
    # The create flow runs detection on a connection + scope, with NO monitor yet.
    cid = ConnectionService(container.metadata, container.secrets).create(
        ctx, source_type="local", name="dw2", coordinates=COORDS, auth_method="none",
    )["id"]
    svc = LoadTypeDetectorService(container.metadata, container.secrets)
    run = svc.start(ctx, connection_id=cid,
                    scope={"database": "DW", "schema": "SRC", "tables": ["CUSTOMER_DIM", "EVENTS"]})
    assert run["monitor_id"] is None and run["connection_id"] == cid
    svc.run_detection(ctx, run["id"])
    done = svc.get(ctx, run["id"])
    assert done["status"] == "done" and len(done["results"]) == 2


def test_unknown_run_is_not_found(container, ctx):
    svc = LoadTypeDetectorService(container.metadata, container.secrets)
    import pytest
    from dvp.core.errors import NotFoundError
    with pytest.raises(NotFoundError):
        svc.get(ctx, "no-such-run")
