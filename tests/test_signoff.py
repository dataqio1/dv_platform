"""A finished migration batch becomes a sign-off the Reports page can reopen."""

from __future__ import annotations

API = "/api/v1"


def _cols():
    return [
        {"name": "ID", "data_type": "NUMBER", "nullable": False},
        {"name": "V", "data_type": "NUMBER", "nullable": False},
    ]


def _conn(client, name, orders, customers):
    return client.post(
        f"{API}/connections",
        json={
            "source_type": "local",
            "name": name,
            "auth_method": "none",
            "coordinates": {
                "database": "D",
                "schema": "S",
                "tables": {
                    "ORDERS": {"columns": _cols(), "rows": orders},
                    "CUSTOMERS": {"columns": _cols(), "rows": customers},
                },
            },
        },
    ).json()["data"]["id"]


def test_sign_off_names_the_failing_key_and_reopens_as_the_same_report(client):
    src = _conn(client, "src", [{"ID": 1, "V": 10}], [{"ID": 1, "V": 5}])
    dst = _conn(client, "dst", [{"ID": 1, "V": 10}], [{"ID": 1, "V": 9}])
    batch = client.post(
        f"{API}/compares/batch",
        json={
            "kind": "data",
            "name": "cutover",
            "source": {"connection_id": src},
            "target": {"connection_id": dst},
            "tables": ["ORDERS", "CUSTOMERS"],
            "options": {"key": ["ID"]},
        },
    ).json()["data"]
    assert batch["matched"] == 1

    signed = client.post(f"{API}/compares/batches/{batch['batch_id']}:sign-off").json()["data"]
    assert signed["name"] == "cutover sign-off"
    assert signed["status"] == "ready"
    assert signed["latest_version"] == 1

    report = client.get(f"{API}/reports/{signed['id']}").json()["data"]
    sign = report["document"]["sign_off"]
    assert sign["ran_by"] == "Local User"
    assert sign["ran_at"]
    by_table = {t["table"]: t for t in sign["tables"]}
    assert by_table["ORDERS"]["status"] == "pass"
    assert by_table["ORDERS"]["exception_keys"] == []
    assert by_table["CUSTOMERS"]["status"] == "fail"
    assert by_table["CUSTOMERS"]["src_count"] == 1
    assert "ID=1" in by_table["CUSTOMERS"]["exception_keys"]

    listed = client.get(f"{API}/reports").json()["data"]
    assert any(r["id"] == signed["id"] for r in listed)

    csv = client.get(f"{API}/reports/{signed['id']}/export?fmt=csv")
    assert csv.status_code == 200
    assert "Local User" in csv.text
    assert "ID=1" in csv.text
    assert "CUSTOMERS" in csv.text

    again = client.post(f"{API}/compares/batches/{batch['batch_id']}:sign-off").json()["data"]
    assert again["id"] == signed["id"]
    assert again["latest_version"] == 2
