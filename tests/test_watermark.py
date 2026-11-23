"""Watermark detection & validation — pure logic + end-to-end API (no mocks)."""

from __future__ import annotations

from dvp.observability.watermark import ColumnInfo, detect_watermark, validate_watermark

API = "/api/v1"


# ----------------------------- pure logic -----------------------------
def _schema(*cols: tuple[str, str, bool]) -> list[ColumnInfo]:
    return [ColumnInfo(name=n, data_type=t, nullable=nul) for n, t, nul in cols]


def test_detect_prefers_full_coverage_load_timestamp():
    schemas = {
        "CUSTOMERS": _schema(
            ("ID", "NUMBER", False),
            ("INGESTED_AT", "TIMESTAMP_NTZ", False),
            ("UPDATED_AT", "TIMESTAMP_NTZ", True),
        ),
        "ORDERS": _schema(
            ("ORDER_ID", "NUMBER", False),
            ("INGESTED_AT", "TIMESTAMP_NTZ", False),
        ),
    }
    suggestions = detect_watermark(schemas)
    assert suggestions[0].column == "INGESTED_AT"
    top = suggestions[0].as_dict()
    assert top["covers_all_tables"] is True
    assert top["type"] == "timestamp"
    assert top["confidence"] > 0.7


def test_detect_ignores_non_temporal_columns():
    schemas = {"T": _schema(("NAME", "VARCHAR", True), ("QTY", "NUMBER", False))}
    # NUMBER is accepted as a numeric watermark; VARCHAR is not.
    cols = {s.column for s in detect_watermark(schemas)}
    assert "NAME" not in cols
    assert "QTY" in cols


def test_detect_empty():
    assert detect_watermark({}) == []


def test_validate_all_tables_have_column():
    schemas = {
        "A": _schema(("INGESTED_AT", "TIMESTAMP_NTZ", False)),
        "B": _schema(("INGESTED_AT", "TIMESTAMP_NTZ", False)),
    }
    res = validate_watermark(schemas, "INGESTED_AT", expected_type="timestamp")
    assert res["valid"] is True
    assert res["tables_ok"] == 2
    assert res["missing_in"] == []


def test_validate_flags_missing_and_type_mismatch():
    schemas = {
        "A": _schema(("INGESTED_AT", "TIMESTAMP_NTZ", False)),
        "B": _schema(("NAME", "VARCHAR", True)),
    }
    res = validate_watermark(schemas, "INGESTED_AT", expected_type="timestamp")
    assert res["valid"] is False
    assert res["missing_in"] == ["B"]


def test_validate_nullable_is_a_warning_not_a_blocker():
    # A nullable watermark is usable — it must surface as a warning, keep the table ok,
    # and NOT invalidate the monitor (regression: nullable used to fail every table).
    schemas = {"A": _schema(("UPDATED_AT", "TIMESTAMP_NTZ", True))}
    res = validate_watermark(schemas, "UPDATED_AT")
    row = res["tables"][0]
    assert row["nullable"] is True
    assert any("nullable" in w for w in row["warnings"])
    assert row["issues"] == []
    assert row["ok"] is True
    assert res["valid"] is True
    assert res["tables_with_warnings"] == 1


# ----------------------------- end-to-end API -----------------------------
def _inline_connection(client) -> str:
    coords = {
        "database": "RAW_SALES",
        "schema": "PUBLIC",
        "tables": {
            "RAW_CUSTOMERS": {
                "columns": [
                    {"name": "ID", "data_type": "NUMBER", "nullable": False},
                    {"name": "INGESTED_AT", "data_type": "TIMESTAMP_NTZ", "nullable": False},
                    {"name": "EMAIL", "data_type": "VARCHAR", "nullable": True},
                ],
                "row_count": 5000,
                "watermarks": {"INGESTED_AT": "2026-09-18T10:45:00"},
            },
            "RAW_ORDERS": {
                "columns": [
                    {"name": "ORDER_ID", "data_type": "NUMBER", "nullable": False},
                    {"name": "INGESTED_AT", "data_type": "TIMESTAMP_NTZ", "nullable": False},
                ],
                "row_count": 12000,
                "watermarks": {"INGESTED_AT": "2026-09-20T02:00:00"},
            },
        },
    }
    return client.post(
        f"{API}/connections",
        json={"source_type": "inline", "name": "raw", "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]


def test_detect_watermark_endpoint(client):
    cid = _inline_connection(client)
    r = client.post(
        f"{API}/connections/{cid}/watermark:detect",
        json={"scope": {"tables": ["RAW_CUSTOMERS", "RAW_ORDERS"]}},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["suggested"]["column"] == "INGESTED_AT"
    assert data["suggested"]["covers_all_tables"] is True


def test_validate_watermark_endpoint(client):
    cid = _inline_connection(client)
    ok = client.post(
        f"{API}/connections/{cid}/watermark:validate",
        json={
            "scope": {"tables": ["RAW_CUSTOMERS", "RAW_ORDERS"]},
            "column": "INGESTED_AT",
            "expected_type": "timestamp",
        },
    ).json()["data"]
    assert ok["valid"] is True
    assert ok["tables_ok"] == 2

    missing = client.post(
        f"{API}/connections/{cid}/watermark:validate",
        json={"scope": {"tables": ["RAW_CUSTOMERS", "RAW_ORDERS"]}, "column": "EMAIL"},
    ).json()["data"]
    # EMAIL only exists in one table and isn't temporal.
    assert missing["valid"] is False
    assert "RAW_ORDERS" in missing["missing_in"]


def test_set_monitor_watermark(client):
    cid = _inline_connection(client)
    mon = client.post(
        f"{API}/monitors",
        json={"connection_id": cid, "name": "raw-sales", "scope": {"tables": ["RAW_CUSTOMERS"]}},
    ).json()["data"]
    assert mon["watermark_column"] is None

    updated = client.post(
        f"{API}/monitors/{mon['id']}:watermark",
        json={"watermark_column": "INGESTED_AT", "watermark_type": "timestamp"},
    ).json()["data"]
    assert updated["watermark_column"] == "INGESTED_AT"
    # persisted
    assert (
        client.get(f"{API}/monitors/{mon['id']}").json()["data"]["watermark_column"]
        == "INGESTED_AT"
    )
