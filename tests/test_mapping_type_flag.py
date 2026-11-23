"""suggest-mapping reports per-column type compatibility (canonicalised) so the UI can flag a real
type mismatch (e.g. NUMBER → VARCHAR) — the #1 cause of false 'differences'. No mocks."""

from __future__ import annotations

API = "/api/v1"


def _conn(client):
    coords = {
        "database": "RAW", "schema": "PUBLIC",
        "tables": {
            "SRC": {
                "columns": [
                    {"name": "ID", "data_type": "NUMBER"},
                    {"name": "NAME", "data_type": "VARCHAR"},
                ],
                "rows": [{"ID": 1, "NAME": "a"}],
            },
            "DST": {
                "columns": [
                    {"name": "ID", "data_type": "VARCHAR"},   # type differs from source NUMBER
                    {"name": "NAME", "data_type": "STRING"},  # VARCHAR≡STRING → compatible
                ],
                "rows": [{"ID": "1", "NAME": "a"}],
            },
        },
    }
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "c", "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]


def test_suggest_mapping_flags_type_mismatch(client):
    cid = _conn(client)
    body = {
        "source": {"connection_id": cid, "schema": "PUBLIC", "table": "SRC"},
        "target": {"connection_id": cid, "schema": "PUBLIC", "table": "DST"},
        "mode": "fuzzy", "detect_key": False,
    }
    d = client.post(f"{API}/compares/suggest-mapping", json=body).json()["data"]

    # Per-column types are returned...
    assert d["source_column_types"]["ID"] == "NUMBER"
    assert d["target_column_types"]["ID"] == "VARCHAR"

    by_src = {m["source"]: m for m in d["mappings"]}
    # NUMBER → VARCHAR is a real mismatch; VARCHAR → STRING canonicalises equal.
    assert by_src["ID"]["type_match"] is False
    assert by_src["ID"]["src_type"] == "NUMBER" and by_src["ID"]["dst_type"] == "VARCHAR"
    assert by_src["NAME"]["type_match"] is True
