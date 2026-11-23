"""Four-pillar table observability — pure cards + end-to-end snapshot/read API."""

from __future__ import annotations

from dvp.observability import pillars as P

API = "/api/v1"


# ----------------------------- pure logic -----------------------------
def test_unknown_when_no_metric():
    card = P.volume_card(None, None, {})
    assert card["status"] == "unknown"
    assert card["headline"] == "Not configured yet"


def test_volume_drop_is_warning_then_critical():
    assert P.volume_card(800, 1000, {})["status"] == "warning"  # -20%
    assert P.volume_card(400, 1000, {})["status"] == "critical"  # -60%
    assert P.volume_card(1050, 1000, {})["status"] == "healthy"


def test_unmeasured_pillar_names_the_reason():
    missing = P.freshness_card(None, None, {}, reason="LOAD_TS is not a column on this table")
    assert missing["headline"] == "LOAD_TS is not a column on this table"
    assert missing["configured"] is True
    assert missing["status"] == "unknown"
    assert P.freshness_card(None, None, {})["headline"] == "Not configured yet"
    quality = P.quality_card(None, {}, reason="This connection cannot score null rates")
    assert quality["headline"] == "This connection cannot score null rates"
    assert quality["configured"] is True


def test_freshness_thresholds():
    assert P.freshness_card(3600, None, {})["status"] == "healthy"  # 1h
    assert P.freshness_card(36 * 3600, None, {})["status"] == "warning"  # 36h
    assert P.freshness_card(100 * 3600, None, {})["status"] == "critical"


def test_quality_thresholds():
    assert P.quality_card(99, {})["status"] == "healthy"
    assert P.quality_card(85, {})["status"] == "warning"
    assert P.quality_card(50, {})["status"] == "critical"


def test_worst_does_not_treat_unmeasured_as_healthy():
    # A real problem still wins over a gap.
    assert P.worst([P.Health.HEALTHY, P.Health.UNKNOWN, P.Health.WARNING]) == P.Health.WARNING
    assert P.worst([P.Health.CRITICAL, P.Health.UNKNOWN]) == P.Health.CRITICAL
    assert P.worst([P.Health.UNKNOWN, P.Health.UNKNOWN]) == P.Health.UNKNOWN
    # Two healthy pillars cannot hide two that were never measured.
    assert P.worst([P.Health.HEALTHY, P.Health.HEALTHY, P.Health.UNKNOWN, P.Health.UNKNOWN]) == (
        P.Health.UNKNOWN
    )


def test_score_cannot_be_perfect_with_gaps():
    cards = [
        {"status": "healthy"},
        {"status": "healthy"},
        {"status": "unknown"},
        {"status": "unknown"},
    ]
    assert P.overall_score(cards) == 50
    assert P.overall_score([{"status": "healthy"}] * 4) == 100
    assert P.overall_score([]) == 0


def test_effective_policy_merges_table_override(container, ctx):
    """A table's override (TableMetadata.config['alert_policy']) overlays the monitor default
    field-by-field — a table tightens freshness while inheriting the rest."""
    from dvp.db.models import Monitor, TableMetadata
    from dvp.observability.observability_service import _effective_policy

    mon = Monitor(  # in-memory (not persisted) — _effective_policy only reads .alert_policy
        connection_id="C", name="m", scope={},
        alert_policy={"freshness": {"mode": "staleness", "error_hours": 72}, "volume": {"warn_pct": 20}},
    )
    with container.metadata.session(ctx) as s:
        s.add(TableMetadata(
            org_id=ctx.org_id, group_id=ctx.group_id, table_ref="DB.S.T", load_pattern="append",
            config={"alert_policy": {"freshness": {"error_hours": 6}, "quality": {"warn_below": 95}}},
        ))
        s.flush()

        eff = _effective_policy(s, ctx.group_id, mon, "DB.S.T")
        assert eff["freshness"]["error_hours"] == 6          # overridden
        assert eff["freshness"]["mode"] == "staleness"       # inherited within the pillar
        assert eff["volume"] == {"warn_pct": 20}             # untouched pillar inherited
        assert eff["quality"] == {"warn_below": 95}          # new pillar from override

        # A table with no override just gets the monitor policy.
        assert _effective_policy(s, ctx.group_id, mon, "DB.S.OTHER") == dict(mon.alert_policy)


# ----------------------------- end-to-end API -----------------------------
TABLE = "RAW_SALES.PUBLIC.RAW_CUSTOMERS"


def _monitor(client) -> str:
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "inline", "name": "raw", "auth_method": "none", "coordinates": {}},
    ).json()["data"]
    return client.post(
        f"{API}/monitors",
        json={
            "connection_id": conn["id"],
            "name": "m",
            "scope": {"tables": ["RAW_CUSTOMERS"]},
            "watermark_column": "INGESTED_AT",
            "watermark_type": "timestamp",
        },
    ).json()["data"]["id"]


def test_snapshot_then_pillars(client):
    mid = _monitor(client)
    r = client.post(
        f"{API}/monitors/{mid}/tables/{TABLE}/snapshot",
        json={
            "row_count": 5000,
            "last_load_ts": "2026-09-18T10:45:00Z",
            "columns": [{"name": "EMAIL", "data_type": "STRING", "null_rate": 0.02}],
            "quality_score": 98,
        },
    )
    assert r.status_code == 200
    assert set(r.json()["data"]["pillars_recorded"]) == {"freshness", "volume", "schema", "quality"}

    cards = client.get(f"{API}/monitors/{mid}/tables/{TABLE}/pillars").json()["data"]
    by = {c["pillar"]: c for c in cards["pillars"]}
    assert by["volume"]["headline"] == "5,000 rows"
    assert by["volume"]["status"] == "healthy"
    assert by["schema"]["headline"] == "1 columns"
    assert by["quality"]["headline"] == "98/100"
    assert by["freshness"]["configured"] is True
    assert by["freshness"]["last_load_at"] is not None
    assert cards["watermark_column"] == "INGESTED_AT"
    assert cards["lineage"]["nodes"][0]["type"] == "SOURCE"


def test_pillar_and_column_history(client):
    mid = _monitor(client)
    for i, rc in enumerate([4800, 4900, 5000]):
        client.post(
            f"{API}/monitors/{mid}/tables/{TABLE}/snapshot",
            json={
                "row_count": rc,
                "columns": [{"name": "EMAIL", "data_type": "STRING", "null_rate": 0.01 * (i + 1)}],
                "ts": f"2026-09-1{i + 1}T00:00:00Z",
            },
        )
    hist = client.get(f"{API}/monitors/{mid}/tables/{TABLE}/pillars/volume/history").json()["data"]
    assert [p["value"] for p in hist["points"]] == [4800, 4900, 5000]  # chronological

    cols = client.get(f"{API}/monitors/{mid}/tables/{TABLE}/columns").json()["data"]
    assert any(c["column"] == "EMAIL" for c in cols)

    col_hist = client.get(f"{API}/monitors/{mid}/tables/{TABLE}/columns/EMAIL/history").json()[
        "data"
    ]
    null_points = col_hist["metrics"][P.M_COL_NULL_RATE]
    assert len(null_points) == 3
    assert null_points[0]["value"] < null_points[-1]["value"]  # increasing, chronological


def test_profile_from_inline_connection(client):
    # Inline connection with real schema + watermark high-value → profile computes pillars.
    coords = {
        "database": "RAW_SALES",
        "schema": "PUBLIC",
        "tables": {
            "RAW_CUSTOMERS": {
                "columns": [
                    {"name": "ID", "data_type": "NUMBER", "nullable": False},
                    {"name": "INGESTED_AT", "data_type": "TIMESTAMP_NTZ", "nullable": False},
                ],
                "row_count": 777,
                "watermarks": {"INGESTED_AT": "2026-09-18T10:45:00"},
            }
        },
    }
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "inline", "name": "raw", "auth_method": "none", "coordinates": coords},
    ).json()["data"]
    mid = client.post(
        f"{API}/monitors",
        json={
            "connection_id": conn["id"],
            "name": "m2",
            "scope": {"tables": ["RAW_CUSTOMERS"]},
            "watermark_column": "INGESTED_AT",
            "watermark_type": "timestamp",
        },
    ).json()["data"]["id"]

    prof = client.post(f"{API}/monitors/{mid}/tables/{TABLE}:profile")
    assert prof.status_code == 200

    cards = client.get(f"{API}/monitors/{mid}/tables/{TABLE}/pillars").json()["data"]
    by = {c["pillar"]: c for c in cards["pillars"]}
    assert by["volume"]["headline"] == "777 rows"
    assert by["schema"]["headline"] == "2 columns"
    assert by["freshness"]["configured"] is True

    # table appears in the monitor's table list
    tables = client.get(f"{API}/monitors/{mid}/tables").json()["data"]
    assert any(t["table_ref"] == TABLE for t in tables)


def test_profile_records_freshness_and_quality_from_rows(client):
    coords = {
        "database": "DWH_TEST",
        "schema": "DMS_BRONZE",
        "tables": {
            "BRZ_ORDERS": {
                "columns": [
                    {"name": "ORDER_ID", "data_type": "NUMBER", "nullable": False},
                    {"name": "AMOUNT", "data_type": "NUMBER", "nullable": True},
                    {"name": "LOAD_TS", "data_type": "TIMESTAMP_NTZ", "nullable": False},
                ],
                "rows": [
                    {"ORDER_ID": 1, "AMOUNT": 10, "LOAD_TS": "2026-10-16T12:00:00"},
                    {"ORDER_ID": 2, "AMOUNT": None, "LOAD_TS": "2026-10-16T18:00:00"},
                ],
            }
        },
    }
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "bronze", "auth_method": "none", "coordinates": coords},
    ).json()["data"]
    mid = client.post(
        f"{API}/monitors",
        json={
            "connection_id": conn["id"],
            "name": "bronze",
            "scope": {"database": "DWH_TEST", "schema": "DMS_BRONZE", "tables": ["BRZ_ORDERS"]},
            "watermark_column": "LOAD_TS",
            "watermark_type": "timestamp",
        },
    ).json()["data"]["id"]
    ref = "DWH_TEST.DMS_BRONZE.BRZ_ORDERS"
    assert client.post(f"{API}/monitors/{mid}/tables/{ref}:profile").status_code == 200
    cards = client.get(f"{API}/monitors/{mid}/tables/{ref}/pillars").json()["data"]
    by = {c["pillar"]: c for c in cards["pillars"]}
    assert by["freshness"]["configured"] is True
    assert by["freshness"]["headline"].startswith("Last load")
    assert by["freshness"]["last_load_at"].startswith("2026-10-16T18:00:00")  # newest LOAD_TS row
    assert by["freshness"]["value"] is not None
    assert by["freshness"]["status"] == "critical"  # that load is well in the past
    assert by["quality"]["headline"] == "83/100"  # 1 null AMOUNT across 6 cells → 5/6
    assert by["quality"]["status"] == "warning"


def test_profile_names_a_missing_watermark(client):
    coords = {
        "database": "DWH_TEST",
        "schema": "DMS_BRONZE",
        "tables": {
            "BRZ_ORDERS": {
                "columns": [{"name": "ORDER_ID", "data_type": "NUMBER", "nullable": False}],
                "rows": [{"ORDER_ID": 1}],
            }
        },
    }
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "bronze2", "auth_method": "none", "coordinates": coords},
    ).json()["data"]
    mid = client.post(
        f"{API}/monitors",
        json={
            "connection_id": conn["id"],
            "name": "bronze2",
            "scope": {"database": "DWH_TEST", "schema": "DMS_BRONZE", "tables": ["BRZ_ORDERS"]},
            "watermark_column": "LOAD_TS",
            "watermark_type": "timestamp",
        },
    ).json()["data"]["id"]
    ref = "DWH_TEST.DMS_BRONZE.BRZ_ORDERS"
    assert client.post(f"{API}/monitors/{mid}/tables/{ref}:profile").status_code == 200
    cards = client.get(f"{API}/monitors/{mid}/tables/{ref}/pillars").json()["data"]
    by = {c["pillar"]: c for c in cards["pillars"]}
    assert by["freshness"]["headline"] == "LOAD_TS is not a column on this table"
    assert by["freshness"]["configured"] is True
    assert "Not configured" not in by["freshness"]["headline"]


def test_orderdate_is_offered_as_the_load_time_until_someone_confirms_it(client):
    assert P.suggest_watermark([{"name": "O_COMMENT"}, {"name": "O_ORDERDATE", "data_type": "DATE"}]) == {
        "column": "O_ORDERDATE",
        "type": "date",
    }
    assert P.suggest_watermark([{"name": "AMOUNT"}, {"name": "ORDER_ID"}]) is None
    assert P.suggest_watermark([{"name": "LOAD_TS", "data_type": "TIMESTAMP_NTZ"}])["type"] == "timestamp"

    conn = client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "tpch", "auth_method": "none", "coordinates": {}},
    ).json()["data"]["id"]
    mid = client.post(
        f"{API}/monitors",
        json={
            "connection_id": conn,
            "name": "orders",
            "scope": {"database": "SNOWFLAKE_SAMPLE_DATA", "schema": "TPCH_SF1", "tables": ["ORDERS"]},
        },
    ).json()["data"]["id"]
    ref = "SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS"
    scanned = client.post(
        f"{API}/monitors/{mid}/tables/{ref}/snapshot",
        json={
            "row_count": 1500000,
            "columns": [
                {"name": "O_ORDERKEY", "data_type": "NUMBER"},
                {"name": "O_ORDERDATE", "data_type": "DATE"},
                {"name": "O_COMMENT", "data_type": "STRING"},
            ],
        },
    )
    assert scanned.status_code == 200
    cards = client.get(f"{API}/monitors/{mid}/tables/{ref}/pillars").json()["data"]
    by = {c["pillar"]: c for c in cards["pillars"]}
    assert cards["suggested_watermark"] == {"column": "O_ORDERDATE", "type": "date"}
    assert by["freshness"]["headline"] == "O_ORDERDATE looks like the load time"
    assert by["freshness"]["status"] == "unknown"
    assert client.get(f"{API}/incidents?active_only=true").json()["data"] == []

    client.post(
        f"{API}/monitors/{mid}:watermark",
        json={"watermark_column": "O_ORDERDATE", "watermark_type": "date"},
    )
    confirmed = client.get(f"{API}/monitors/{mid}/tables/{ref}/pillars").json()["data"]
    assert confirmed["watermark_column"] == "O_ORDERDATE"
    assert confirmed["suggested_watermark"] is None
    freshness = {c["pillar"]: c for c in confirmed["pillars"]}["freshness"]
    assert freshness["headline"] == "No scan has read O_ORDERDATE"
    audit = client.get(f"{API}/audit?entity_type=monitor&entity_id={mid}").json()["data"]
    wm = next(r for r in audit if r["action"] == "monitor.watermark")
    assert wm["detail"]["changes"]["watermark_column"]["to"] == "O_ORDERDATE"
    assert wm["detail"]["changes"]["watermark_type"]["to"] == "date"


def test_a_set_watermark_is_not_described_as_unconfigured(client):
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "bronze", "auth_method": "none", "coordinates": {}},
    ).json()["data"]["id"]
    mid = client.post(
        f"{API}/monitors",
        json={
            "connection_id": conn,
            "name": "raw sales",
            "scope": {"database": "DWH_TEST", "schema": "DMS_BRONZE", "tables": ["BRZ_ORDERS"]},
            "watermark_column": "LOAD_TS",
            "watermark_type": "timestamp",
        },
    ).json()["data"]["id"]
    ref = "DWH_TEST.DMS_BRONZE.BRZ_ORDERS"
    scanned = client.post(
        f"{API}/monitors/{mid}/tables/{ref}/snapshot",
        json={"row_count": 19879, "columns": [{"name": "ORDER_ID", "data_type": "NUMBER"}]},
    )
    assert scanned.status_code == 200
    cards = client.get(f"{API}/monitors/{mid}/tables/{ref}/pillars").json()["data"]
    by = {c["pillar"]: c for c in cards["pillars"]}
    assert by["freshness"]["status"] == "unknown"
    assert by["freshness"]["headline"] == "No scan has read LOAD_TS"
    assert by["freshness"]["configured"] is True
    assert cards["health"] == "unknown"
    incidents = client.get(f"{API}/incidents?active_only=true").json()["data"]
    assert incidents == []


def test_a_scan_that_did_not_score_quality_is_not_called_unconfigured(client):
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "bronze", "auth_method": "none", "coordinates": {}},
    ).json()["data"]["id"]
    mid = client.post(
        f"{API}/monitors",
        json={
            "connection_id": conn,
            "name": "raw sales",
            "scope": {"database": "DWH_TEST", "schema": "DMS_BRONZE", "tables": ["BRZ_ORDERS"]},
        },
    ).json()["data"]["id"]
    ref = "DWH_TEST.DMS_BRONZE.BRZ_ORDERS"
    scanned = client.post(
        f"{API}/monitors/{mid}/tables/{ref}/snapshot",
        json={"row_count": 19879, "columns": [{"name": "ORDER_ID", "data_type": "NUMBER"}]},
    )
    assert scanned.status_code == 200
    cards = client.get(f"{API}/monitors/{mid}/tables/{ref}/pillars").json()["data"]
    by = {c["pillar"]: c for c in cards["pillars"]}
    assert by["quality"]["status"] == "unknown"
    assert by["quality"]["headline"] == "No scan has scored quality"
    assert by["quality"]["configured"] is True
    assert by["volume"]["status"] == "healthy"

    checked = client.post(
        f"{API}/monitors/{mid}/tables/{ref}/snapshot",
        json={"row_count": 19879, "query_shape": "check"},
    )
    assert checked.status_code == 200
    again = client.get(f"{API}/monitors/{mid}/tables/{ref}/pillars").json()["data"]
    quality = {c["pillar"]: c for c in again["pillars"]}["quality"]
    assert quality["headline"] == "The last check did not score columns"
    assert quality["status"] == "unknown"
    assert client.get(f"{API}/incidents?active_only=true").json()["data"] == []


def test_diff_names_added_removed_and_type_change():
    from dvp.observability.pillars import describe_schema_changes, diff_columns

    prior = [
        {"name": "EMAIL", "data_type": "VARCHAR"},
        {"name": "ID", "data_type": "NUMBER"},
        {"name": "OLD_FLAG", "data_type": "BOOLEAN"},
    ]
    current = [
        {"name": "EMAIL", "data_type": "VARIANT"},
        {"name": "ID", "data_type": "NUMBER"},
        {"name": "REGION", "data_type": "VARCHAR"},
    ]
    changes = diff_columns(prior, current)
    assert changes["added"] == ["REGION"]
    assert changes["removed"] == ["OLD_FLAG"]
    assert changes["type_changed"] == [{"column": "EMAIL", "from": "VARCHAR", "to": "VARIANT"}]
    text = describe_schema_changes(changes, "BRZ_CUSTOMERS")
    assert "EMAIL changed from VARCHAR to VARIANT on BRZ_CUSTOMERS" in text
    assert "REGION added on BRZ_CUSTOMERS" in text
    assert "OLD_FLAG removed on BRZ_CUSTOMERS" in text


def test_schema_snapshot_names_columns_and_other_users(client):
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "snowflake", "name": "sf", "coordinates": {}, "auth_method": "key_pair"},
    ).json()["data"]
    scope = {"database": "DWH", "schema": "PUBLIC", "tables": ["BRZ_CUSTOMERS"]}
    mid = client.post(
        f"{API}/monitors",
        json={"connection_id": conn["id"], "name": "bronze", "scope": scope},
    ).json()["data"]["id"]
    client.post(
        f"{API}/monitors",
        json={"connection_id": conn["id"], "name": "gold customers", "scope": scope},
    )
    client.post(
        f"{API}/compares",
        json={
            "kind": "schema",
            "name": "customers cutover",
            "source": {"connection_id": conn["id"], "schema": "PUBLIC", "table": "BRZ_CUSTOMERS"},
            "target": {"connection_id": conn["id"], "schema": "PUBLIC", "table": "BRZ_CUSTOMERS"},
        },
    )
    ref = "DWH.PUBLIC.BRZ_CUSTOMERS"
    cols = [
        {"name": "EMAIL", "data_type": "VARCHAR"},
        {"name": "ID", "data_type": "NUMBER"},
    ]
    first = client.post(
        f"{API}/monitors/{mid}/tables/{ref}/snapshot",
        json={"row_count": 10, "columns": cols},
    )
    assert first.status_code == 200
    assert client.get(f"{API}/incidents?active_only=true").json()["data"] == []

    changed = [
        {"name": "EMAIL", "data_type": "VARIANT"},
        {"name": "ID", "data_type": "NUMBER"},
        {"name": "REGION", "data_type": "VARCHAR"},
    ]
    second = client.post(
        f"{API}/monitors/{mid}/tables/{ref}/snapshot",
        json={"row_count": 10, "columns": changed},
    )
    assert second.status_code == 200
    cards = client.get(f"{API}/monitors/{mid}/tables/{ref}/pillars").json()["data"]
    schema = next(c for c in cards["pillars"] if c["pillar"] == "schema")
    assert "EMAIL changed from VARCHAR to VARIANT" in schema["detail"]
    assert "REGION added" in schema["detail"]

    incidents = client.get(f"{API}/incidents?active_only=true").json()["data"]
    drift = next(i for i in incidents if i["kind"] == "schema_drift")
    assert drift["summary"] == (
        "REGION added on BRZ_CUSTOMERS; EMAIL changed from VARCHAR to VARIANT on BRZ_CUSTOMERS"
    )
    used = drift["evidence"]["also_used_by"]
    assert "monitor gold customers" in used
    assert "test case customers cutover" in used


def test_a_schema_with_no_types_is_not_called_healthy(client):
    from dvp.observability.pillars import type_gap_detail

    assert type_gap_detail(
        [{"name": "AMOUNT", "data_type": "NUMBER"}, {"name": "STATUS"}]
    ) == "STATUS has no type, so a type change there would be missed"
    conn = client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "bronze", "auth_method": "none", "coordinates": {}},
    ).json()["data"]["id"]
    mid = client.post(
        f"{API}/monitors",
        json={
            "connection_id": conn,
            "name": "raw sales",
            "scope": {"database": "DWH_TEST", "schema": "DMS_BRONZE", "tables": ["BRZ_ORDERS"]},
        },
    ).json()["data"]["id"]
    ref = "DWH_TEST.DMS_BRONZE.BRZ_ORDERS"
    names = [
        "ORDER_ID",
        "CUSTOMER_ID",
        "PRODUCT_ID",
        "ORDER_TS",
        "AMOUNT",
        "STATUS",
        "SRC_FILE",
        "LOAD_TS",
        "BATCH_ID",
    ]
    scanned = client.post(
        f"{API}/monitors/{mid}/tables/{ref}/snapshot",
        json={"row_count": 19879, "columns": [{"name": name} for name in names]},
    )
    assert scanned.status_code == 200
    cards = client.get(f"{API}/monitors/{mid}/tables/{ref}/pillars").json()["data"]
    schema = next(c for c in cards["pillars"] if c["pillar"] == "schema")
    assert schema["status"] == "unknown"
    assert schema["headline"] == "9 columns"
    assert schema["detail"] == "Types were not recorded, so a type change would be missed"
    assert schema["configured"] is True
    listed = client.get(f"{API}/monitors/{mid}/tables").json()["data"]
    assert listed[0]["pillars"]["schema"] == "unknown"
    cols = client.get(f"{API}/monitors/{mid}/tables/{ref}/columns").json()["data"]
    assert cols[0]["column"] == "ORDER_ID"
    assert cols[0]["data_type"] is None
    assert client.get(f"{API}/incidents?active_only=true").json()["data"] == []

    typed = client.post(
        f"{API}/monitors/{mid}/tables/{ref}/snapshot",
        json={
            "row_count": 19879,
            "columns": [{"name": name, "data_type": "VARCHAR"} for name in names],
        },
    )
    assert typed.status_code == 200
    again = client.get(f"{API}/monitors/{mid}/tables/{ref}/pillars").json()["data"]
    schema = next(c for c in again["pillars"] if c["pillar"] == "schema")
    assert schema["status"] == "healthy"
    assert schema["detail"] == "no changes since last run"
    assert client.get(f"{API}/incidents?active_only=true").json()["data"] == []
