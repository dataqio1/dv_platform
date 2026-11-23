"""UX bundle: full-differences CSV export + bulk project rerun. Real API, no mocks."""

from __future__ import annotations

API = "/api/v1"

COLS = [{"name": "ORDER_ID", "data_type": "NUMBER"}, {"name": "AMOUNT", "data_type": "NUMBER"}]


def _conn(client, name, amount=10):
    coords = {
        "database": "DB", "schema": "PUBLIC",
        "tables": {"ORDERS": {"columns": COLS, "rows": [
            {"ORDER_ID": 1, "AMOUNT": amount}, {"ORDER_ID": 2, "AMOUNT": 20},
            {"ORDER_ID": 3, "AMOUNT": 30},
        ]}},
    }
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": name, "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]


def _job(client, src, dst, project_id=None, name="cmp"):
    body = {
        "kind": "data", "name": name,
        "source": {"connection_id": src, "table": "ORDERS"},
        "target": {"connection_id": dst, "table": "ORDERS"},
        "options": {"key": ["ORDER_ID"]},
    }
    if project_id:
        body["project_id"] = project_id
    j = client.post(f"{API}/compares", json=body).json()["data"]["id"]
    client.post(f"{API}/compares/{j}:run")
    return j


def test_differences_csv_export(client):
    # Source AMOUNT=10 vs target AMOUNT=999 on ORDER_ID=1 → one value difference.
    src, dst = _conn(client, "csv-src", amount=10), _conn(client, "csv-dst", amount=999)
    jid = _job(client, src, dst)

    r = client.get(f"{API}/compares/{jid}/differences/export")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    body = r.text
    assert body.splitlines()[0] == "bucket,key,column,source,target"
    # The AMOUNT difference on ORDER_ID=1 is present with both values.
    assert "value_difference" in body
    assert "ORDER_ID=1" in body and "AMOUNT" in body
    assert "10" in body and "999" in body


def test_bulk_rerun_only_failed(client):
    p = client.post(f"{API}/projects", json={"name": "BulkProj"}).json()["data"]["id"]
    # One passing test case and one differing one.
    good_s = _conn(client, "ok-s", amount=10)
    good_t = _conn(client, "ok-t", amount=10)
    bad_s = _conn(client, "bad-s", amount=10)
    bad_t = _conn(client, "bad-t", amount=500)
    good = _job(client, good_s, good_t, project_id=p, name="healthy")
    _job(client, bad_s, bad_t, project_id=p, name="broken")  # differs → counts as failed

    # Default only_failed=true → only the differing test case is requeued.
    r = client.post(f"{API}/compares/projects/{p}:rerun")
    assert r.status_code == 202
    data = r.json()["data"]
    assert data["queued"] == 1

    # The healthy one kept its prior succeeded result; the differing one was requeued.
    assert client.get(f"{API}/compares/{good}").json()["data"]["status"] == "succeeded"

    # only_failed=false requeues BOTH.
    r2 = client.post(f"{API}/compares/projects/{p}:rerun?only_failed=false")
    assert r2.json()["data"]["queued"] == 2
