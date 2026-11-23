"""Named ETL metadata conventions + multi-convention (coverage-based) resolution over the REAL
local SQL connector (no mocks). Mirrors the customer reality: many connections share one ETL
tool's convention, and a single connection whose schema is loaded by TWO tools (IDMC-CDC + an
S3->Snowflake loader) resolves each table to the right convention automatically.
"""

from __future__ import annotations

from dvp.services.table_metadata import resolve_conventions

API = "/api/v1"

# A connection whose schema is populated by two different ETL tools:
#  ACCOUNT  -> IDMC CDC    (AUDIT_UPDATE_TS, TASK_RUN_ID)
#  FILE_FEED-> S3 loader   (_LOADED_AT, _FILE_BATCH)
COORDS = {
    "database": "DWH_TEST",
    "schema": "SRC_MIX",
    "tables": {
        "ACCOUNT": {
            "columns": [
                {"name": "ACCOUNT_ID", "data_type": "NUMBER", "nullable": False},
                {"name": "STATUS", "data_type": "VARCHAR", "nullable": True},
                {"name": "AUDIT_UPDATE_TS", "data_type": "TIMESTAMP", "nullable": False},
                {"name": "TASK_RUN_ID", "data_type": "NUMBER", "nullable": False},
            ],
            "rows": [{"ACCOUNT_ID": 1, "STATUS": "OPEN", "AUDIT_UPDATE_TS": "2026-11-05T00:00:00", "TASK_RUN_ID": 7}],
        },
        "FILE_FEED": {
            "columns": [
                {"name": "ROW_ID", "data_type": "NUMBER", "nullable": False},
                {"name": "PAYLOAD", "data_type": "VARCHAR", "nullable": True},
                {"name": "_LOADED_AT", "data_type": "TIMESTAMP", "nullable": False},
                {"name": "_FILE_BATCH", "data_type": "VARCHAR", "nullable": False},
            ],
            "rows": [{"ROW_ID": 1, "PAYLOAD": "x", "_LOADED_AT": "2026-11-05T00:00:00", "_FILE_BATCH": "b1"}],
        },
    },
}

IDMC = {"updated_at": "AUDIT_UPDATE_TS", "batch_id": "TASK_RUN_ID"}
S3 = {"loaded_at": "_LOADED_AT", "batch_id": "_FILE_BATCH"}


# ----------------------------- pure resolver -----------------------------
def test_resolver_picks_best_covering_convention():
    convs = [{"id": "c1", "name": "IDMC-CDC", "roles": IDMC}, {"id": "c2", "name": "S3-Loader", "roles": S3}]
    acct = [{"name": "ACCOUNT_ID"}, {"name": "AUDIT_UPDATE_TS"}, {"name": "TASK_RUN_ID"}]
    feed = [{"name": "ROW_ID"}, {"name": "_LOADED_AT"}, {"name": "_FILE_BATCH"}]
    assert resolve_conventions(convs, acct)["matched"]["name"] == "IDMC-CDC"
    assert resolve_conventions(convs, feed)["matched"]["name"] == "S3-Loader"
    assert resolve_conventions(convs, [{"name": "ID"}])["matched"] is None  # none → auto-detect


# ----------------------------- convention library API -----------------------------
def test_convention_library_lists_presets_and_user_defined(client):
    lib = client.get(f"{API}/metadata-conventions").json()["data"]
    ids = {c["id"] for c in lib}
    assert any(i.startswith("sys:") for i in ids)  # shipped presets present
    assert "sys:dbt_snapshot" in ids

    created = client.post(f"{API}/metadata-conventions",
                          json={"name": "IDMC-CDC", "roles": IDMC}).json()["data"]
    assert created["id"] and created["system"] is False
    lib2 = {c["id"]: c for c in client.get(f"{API}/metadata-conventions").json()["data"]}
    assert created["id"] in lib2 and lib2[created["id"]]["name"] == "IDMC-CDC"


def test_shipped_conventions_are_read_only(client):
    r = client.put(f"{API}/metadata-conventions/sys:dbt_snapshot", json={"name": "x"})
    assert r.status_code >= 400  # cannot edit a system preset


# ----------------------------- the two-ETL-tool case, end to end -----------------------------
def test_mixed_tool_connection_resolves_each_table_to_its_convention(client):
    idmc = client.post(f"{API}/metadata-conventions", json={"name": "IDMC-CDC", "roles": IDMC}).json()["data"]
    s3 = client.post(f"{API}/metadata-conventions", json={"name": "S3-Loader", "roles": S3}).json()["data"]

    # One connection, BOTH conventions attached (a schema loaded by two tools).
    cid = client.post(f"{API}/connections", json={
        "source_type": "local", "name": "mix", "auth_method": "none", "coordinates": COORDS,
        "metadata_convention_ids": [idmc["id"], s3["id"]],
    }).json()["data"]
    assert set(cid["metadata_convention_ids"]) == {idmc["id"], s3["id"]}
    conn_id = cid["id"]

    mon = client.post(f"{API}/monitors", json={
        "connection_id": conn_id, "name": "mixed",
        "scope": {"database": "DWH_TEST", "schema": "SRC_MIX", "tables": ["ACCOUNT", "FILE_FEED"]},
    }).json()["data"]
    assert set(mon["metadata_convention_ids"]) == {idmc["id"], s3["id"]}  # inherited from connection

    d = client.post(f"{API}/monitors/{mon['id']}:detect-load-type").json()["data"]
    by_table = {r["table_ref"]: r for r in d["results"]}
    acct, feed = by_table["ACCOUNT"], by_table["FILE_FEED"]

    # Each table resolved to the convention whose columns it actually has — automatically.
    assert acct["matched_convention"]["name"] == "IDMC-CDC"
    assert acct["load_pattern"] == "cdc"
    assert feed["matched_convention"]["name"] == "S3-Loader"
    assert feed["load_pattern"] == "append"


def test_shared_convention_reused_across_connections(client):
    idmc = client.post(f"{API}/metadata-conventions", json={"name": "IDMC-CDC", "roles": IDMC}).json()["data"]
    ids = []
    for n in ("conn-a", "conn-b"):
        c = client.post(f"{API}/connections", json={
            "source_type": "local", "name": n, "auth_method": "none", "coordinates": COORDS,
            "metadata_convention_ids": [idmc["id"]],
        }).json()["data"]
        ids.append(c["metadata_convention_ids"])
    # The same convention id is referenced by both connections (edit once → both update).
    assert ids[0] == ids[1] == [idmc["id"]]
