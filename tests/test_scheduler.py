"""Scheduler runs active monitors end-to-end (profile + evaluate) — no mocks."""

from __future__ import annotations

from dvp.observability.observability_service import (
    RuleEvaluationService,
    TableObservabilityService,
)
from dvp.observability.scheduler import Scheduler
from dvp.observability.service import MonitorService, RuleService
from dvp.services.connections import ConnectionService

COORDS = {
    "database": "RAW_SALES",
    "schema": "PUBLIC",
    "tables": {
        "RAW_ORDERS": {
            "columns": [
                {"name": "ORDER_ID", "data_type": "NUMBER", "nullable": False},
                {"name": "AMOUNT", "data_type": "NUMBER", "nullable": False},
                {"name": "INGESTED_AT", "data_type": "TIMESTAMP_NTZ", "nullable": False},
            ],
            "rows": [
                {"ORDER_ID": 1, "AMOUNT": 10, "INGESTED_AT": "2026-10-01T00:00:00"},
                {"ORDER_ID": 2, "AMOUNT": 20, "INGESTED_AT": "2026-10-02T00:00:00"},
            ],
        }
    },
}
TABLE = "RAW_SALES.PUBLIC.RAW_ORDERS"


def test_scheduler_run_once(container, ctx, settings):
    conn = ConnectionService(container.metadata, container.secrets).create(
        ctx, source_type="local", name="orders", coordinates=COORDS, auth_method="none"
    )
    mid = MonitorService(container.metadata).create(
        ctx,
        connection_id=conn["id"],
        name="orders",
        scope={"database": "RAW_SALES", "schema": "PUBLIC", "tables": ["RAW_ORDERS"]},
        watermark_column="INGESTED_AT",
        schedule_cron="* * * * *",
    )["id"]
    rid = RuleService(container.metadata).create(
        ctx,
        mid,
        {
            "name": "has_rows",
            "type": "row_count",
            "table_ref": TABLE,
            "config": {"min_rows": 1},
            "state": "enabled",
        },
    )["id"]

    MonitorService(container.metadata).create(
        ctx,
        connection_id=conn["id"],
        name="manual",
        scope={"database": "RAW_SALES", "schema": "PUBLIC", "tables": ["RAW_ORDERS"]},
    )
    ran = Scheduler(container, settings, ctx).run_once()
    assert ran == 1

    # pillars were profiled from real data
    cards = TableObservabilityService(container.metadata).get_table_pillars(ctx, mid, TABLE)
    by = {c["pillar"]: c for c in cards["pillars"]}
    assert by["volume"]["headline"] == "2 rows"
    assert cards["query_shape"] == "check"
    assert "max_watermark" in cards["queries"]
    assert "count_star_and_null_rates" not in cards["queries"]
    # a scheduled check doesn't score quality, but the pillar stays configured (shows why).
    assert by["quality"]["configured"] is True
    assert by["quality"]["status"] == "unknown"
    assert by["quality"]["headline"] == "The last check did not score columns"

    # the rule was evaluated and recorded
    hist = RuleEvaluationService(container.metadata).history(ctx, mid, rid)
    assert hist["points"]
    assert hist["points"][-1]["value"] == 2
    assert hist["points"][-1]["passed"] is True
