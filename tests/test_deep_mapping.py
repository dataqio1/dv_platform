"""Deep column mapping — match columns by DATA, not names (real local SQLite, no mocks).

The stress test: the SAME data appears on both sides, but the target is a SQL view that renames
EVERY column to something a name matcher can't recognize. Fuzzy (names) maps nothing; deep (value
sampling) must line every column back up by what the data actually is.
"""

from __future__ import annotations

API = "/api/v1"


def _cols(*specs):
    return [{"name": n, "data_type": t, "nullable": True} for n, t in specs]


# One customers table. Each column has a DISTINCT value domain (ids, names, emails, phones,
# countries, decimals, segments, dates) so there's no ambiguity — a value belongs to exactly one
# column, which is what lets data-similarity re-identify a renamed column.
CUSTOMERS = {
    "columns": _cols(
        ("CUST_ID", "NUMBER"), ("FULL_NAME", "VARCHAR"), ("EMAIL", "VARCHAR"),
        ("PHONE", "VARCHAR"), ("COUNTRY", "VARCHAR"), ("BALANCE", "DECIMAL"),
        ("SEGMENT", "VARCHAR"), ("SIGNUP", "VARCHAR"),
    ),
    "rows": [
        {"CUST_ID": 5001, "FULL_NAME": "Alice Archer",   "EMAIL": "alice@acme.io",   "PHONE": "+1-202-555-0101", "COUNTRY": "US", "BALANCE": 1250.75, "SEGMENT": "RETAIL",    "SIGNUP": "2021-01-04"},
        {"CUST_ID": 5002, "FULL_NAME": "Bob Brennan",    "EMAIL": "bob@acme.io",     "PHONE": "+44-20-7946-0102", "COUNTRY": "UK", "BALANCE": 980.00,  "SEGMENT": "WHOLESALE", "SIGNUP": "2021-02-11"},
        {"CUST_ID": 5003, "FULL_NAME": "Carol Chen",     "EMAIL": "carol@acme.io",   "PHONE": "+1-415-555-0103", "COUNTRY": "CA", "BALANCE": 44210.10, "SEGMENT": "VIP",       "SIGNUP": "2021-03-19"},
        {"CUST_ID": 5004, "FULL_NAME": "Dan Delgado",    "EMAIL": "dan@acme.io",     "PHONE": "+61-2-5550-0104", "COUNTRY": "AU", "BALANCE": 310.25,  "SEGMENT": "RETAIL",    "SIGNUP": "2021-05-02"},
        {"CUST_ID": 5005, "FULL_NAME": "Eve Esposito",   "EMAIL": "eve@acme.io",     "PHONE": "+49-30-5550-0105", "COUNTRY": "DE", "BALANCE": 7788.90, "SEGMENT": "WHOLESALE", "SIGNUP": "2021-06-30"},
        {"CUST_ID": 5006, "FULL_NAME": "Frank Fisher",   "EMAIL": "frank@acme.io",   "PHONE": "+33-1-5550-0106", "COUNTRY": "FR", "BALANCE": 15.00,   "SEGMENT": "VIP",       "SIGNUP": "2021-08-08"},
        {"CUST_ID": 5007, "FULL_NAME": "Grace Guo",      "EMAIL": "grace@acme.io",   "PHONE": "+81-3-5550-0107", "COUNTRY": "JP", "BALANCE": 62000.00, "SEGMENT": "RETAIL",    "SIGNUP": "2021-09-14"},
        {"CUST_ID": 5008, "FULL_NAME": "Hank Huang",     "EMAIL": "hank@acme.io",    "PHONE": "+91-22-5550-0108", "COUNTRY": "IN", "BALANCE": 505.55,  "SEGMENT": "WHOLESALE", "SIGNUP": "2021-11-23"},
    ],
}

# Opaque target names — a different system's schema. NONE of these name-match the source columns
# (no exact / prefix / abbreviation / suffix overlap), so only data can line them up.
RENAMES = {
    "CUST_ID": "party_ref",
    "FULL_NAME": "display_label",
    "EMAIL": "contact_point",
    "PHONE": "msisdn",
    "COUNTRY": "geo",
    "BALANCE": "net_position",
    "SEGMENT": "tier",
    "SIGNUP": "enrolled_on",
}


def _conn(client):
    return client.post(f"{API}/connections", json={
        "source_type": "local", "name": "cust", "auth_method": "none",
        "coordinates": {"database": "DB", "schema": "PUBLIC", "tables": {"CUSTOMERS": CUSTOMERS}},
    }).json()["data"]["id"]


def _renamed_sql(mapping: dict[str, str]) -> str:
    sel = ", ".join(f"{src} AS {tgt}" for src, tgt in mapping.items())
    return f"SELECT {sel} FROM CUSTOMERS"


def _suggest(client, src, tgt, mode):
    return client.post(
        f"{API}/compares/suggest-mapping", json={"source": src, "target": tgt, "mode": mode}
    ).json()["data"]


def test_fuzzy_cannot_map_fully_renamed_columns(client):
    """Names only: when the target renames every column to something unrelated, fuzzy maps NONE
    of them — they all come back unmapped. (This is the baseline deep has to beat.)"""
    cid = _conn(client)
    src = {"connection_id": cid, "table": "CUSTOMERS"}
    tgt = {"connection_id": cid, "sql": _renamed_sql(RENAMES)}

    r = _suggest(client, src, tgt, "fuzzy")
    assert r["mode"] == "fuzzy"
    assert r["mappings"] == []
    assert set(r["unmapped_source"]) == set(RENAMES.keys())


def test_deep_maps_every_fully_renamed_column_by_data(client):
    """Deep: the SAME data behind opaque names is re-identified column-by-column — all 8 lined up
    purely by value overlap, each via DATA_SIMILARITY with a real probability."""
    cid = _conn(client)
    src = {"connection_id": cid, "table": "CUSTOMERS"}
    tgt = {"connection_id": cid, "sql": _renamed_sql(RENAMES)}

    r = _suggest(client, src, tgt, "deep")
    assert r["mode"] == "deep"
    mapped = {m["source"]: m["target"] for m in r["mappings"]}
    assert mapped == RENAMES  # every source column found its renamed twin
    assert all(m["method"] == "DATA_SIMILARITY" for m in r["mappings"])
    # Identical data → high confidence; never a wild guess (threshold is 0.30).
    assert all(m["confidence"] > 0.30 for m in r["mappings"])
    assert not r["unmapped_source"]


def test_deep_does_not_cross_wire_distinct_columns(client):
    """A column must map to the twin holding ITS values — not some other column's. We shuffle the
    target's SELECT order so position can't accidentally be right; data still wins."""
    cid = _conn(client)
    src = {"connection_id": cid, "table": "CUSTOMERS"}
    # reversed column order in the SELECT — position no longer lines up with the source
    shuffled = dict(reversed(list(RENAMES.items())))
    tgt = {"connection_id": cid, "sql": _renamed_sql(shuffled)}

    r = _suggest(client, src, tgt, "deep")
    mapped = {m["source"]: m["target"] for m in r["mappings"]}
    # Each source still maps to the alias carrying its own data, regardless of SELECT order.
    assert mapped == RENAMES
    # e.g. the numeric id did not get pulled to the numeric balance column
    assert mapped["CUST_ID"] == "party_ref" and mapped["BALANCE"] == "net_position"


# Two tables with the SAME KINDS of columns but ZERO shared values — as if sampled from different
# rows of two huge tables. Value-overlap is 0, so only sample-invariant PROFILES (type + semantic
# shape + length/magnitude) can line them up. Each column uses a DISTINCTIVE pattern.
SRC_T = {
    "columns": _cols(("ORDER_ID", "NUMBER"), ("EMAIL", "VARCHAR"), ("PHONE", "VARCHAR"),
                     ("CREATED", "VARCHAR"), ("AMOUNT", "DECIMAL")),
    "rows": [
        {"ORDER_ID": 1001, "EMAIL": "alice@acme.io", "PHONE": "+1-202-555-0101", "CREATED": "2021-01-04", "AMOUNT": 12.50},
        {"ORDER_ID": 1002, "EMAIL": "bob@acme.io",   "PHONE": "+1-202-555-0102", "CREATED": "2021-02-09", "AMOUNT": 20.00},
        {"ORDER_ID": 1003, "EMAIL": "carol@acme.io", "PHONE": "+1-202-555-0103", "CREATED": "2021-03-15", "AMOUNT": 33.75},
        {"ORDER_ID": 1004, "EMAIL": "dan@acme.io",   "PHONE": "+1-202-555-0104", "CREATED": "2021-04-21", "AMOUNT": 45.10},
        {"ORDER_ID": 1005, "EMAIL": "eve@acme.io",   "PHONE": "+1-202-555-0105", "CREATED": "2021-05-30", "AMOUNT": 58.90},
    ],
}
TGT_T = {  # opaque renamed columns, COMPLETELY different values (no overlap with SRC_T)
    "columns": _cols(("alpha", "NUMBER"), ("bravo", "VARCHAR"), ("charlie", "VARCHAR"),
                     ("delta", "VARCHAR"), ("echo", "DECIMAL")),
    "rows": [
        {"alpha": 7001, "bravo": "zoe@globex.com",    "charlie": "+44-20-7946-7001", "delta": "2023-07-01", "echo": 880.25},
        {"alpha": 7002, "bravo": "yan@globex.com",    "charlie": "+44-20-7946-7002", "delta": "2023-08-12", "echo": 915.40},
        {"alpha": 7003, "bravo": "xavier@globex.com", "charlie": "+44-20-7946-7003", "delta": "2023-09-19", "echo": 1020.00},
        {"alpha": 7004, "bravo": "wendy@globex.com",  "charlie": "+44-20-7946-7004", "delta": "2023-10-25", "echo": 1130.60},
        {"alpha": 7005, "bravo": "victor@globex.com", "charlie": "+44-20-7946-7005", "delta": "2023-11-30", "echo": 1245.15},
    ],
}


def _conn_t(client, name, table, spec):
    return client.post(f"{API}/connections", json={
        "source_type": "local", "name": name, "auth_method": "none",
        "coordinates": {"database": "DB", "schema": "PUBLIC", "tables": {table: spec}},
    }).json()["data"]["id"]


def test_deep_profile_matches_when_values_do_not_overlap(client):
    """The robustness case: two tables share NO values (different-row samples), so value-overlap is
    0 for every column. The sample-invariant profile — type + semantic shape (id/email/phone/date/
    amount) + length/magnitude — still lines every column up, via PROFILE_MATCH."""
    sc = _conn_t(client, "src_t", "T", SRC_T)
    tc = _conn_t(client, "tgt_t", "T", TGT_T)
    src = {"connection_id": sc, "table": "T"}
    tgt = {"connection_id": tc, "table": "T"}

    # Fuzzy can't (opaque names); plain value-overlap can't either (nothing overlaps).
    fuzzy = _suggest(client, src, tgt, "fuzzy")
    assert fuzzy["mappings"] == []

    deep = _suggest(client, src, tgt, "deep")
    mapped = {m["source"]: m["target"] for m in deep["mappings"]}
    assert mapped == {"ORDER_ID": "alpha", "EMAIL": "bravo", "PHONE": "charlie", "CREATED": "delta", "AMOUNT": "echo"}
    # It was the PROFILE that matched them (no value overlap existed), and the numeric id did not
    # get cross-wired to the decimal amount despite both being numeric.
    assert all(m["method"] == "PROFILE_MATCH" for m in deep["mappings"])
    assert all(0 < m["confidence"] <= 1 for m in deep["mappings"])


def test_deep_hybrid_names_where_they_match_data_for_the_rest(client):
    """Realistic ETL: some columns keep their name, others are renamed. Deep keeps the exact-name
    matches AND recovers the renamed ones from data — the best of both."""
    cid = _conn(client)
    src = {"connection_id": cid, "table": "CUSTOMERS"}
    # keep EMAIL + COUNTRY names; rename the rest to opaque aliases
    mixed = dict(RENAMES)
    mixed["EMAIL"] = "EMAIL"
    mixed["COUNTRY"] = "COUNTRY"
    tgt = {"connection_id": cid, "sql": _renamed_sql(mixed)}

    r = _suggest(client, src, tgt, "deep")
    by = {m["source"]: m for m in r["mappings"]}
    assert by["EMAIL"]["target"] == "EMAIL" and by["EMAIL"]["method"] == "EXACT_MATCH"
    assert by["COUNTRY"]["target"] == "COUNTRY" and by["COUNTRY"]["method"] == "EXACT_MATCH"
    assert by["CUST_ID"]["target"] == "party_ref" and by["CUST_ID"]["method"] == "DATA_SIMILARITY"
    assert by["BALANCE"]["target"] == "net_position" and by["BALANCE"]["method"] == "DATA_SIMILARITY"
    assert {m["source"]: m["target"] for m in r["mappings"]} == mixed
