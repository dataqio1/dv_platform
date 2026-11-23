"""Property tests for the Find Column pruning cascade (metadata gates before the Bloom).

The invariant the whole design rests on: a gate may KEEP a column that doesn't contain the value
(false positive, harmless), but must NEVER reject a column that DOES (false negative, breaks
search). We verify that two ways:
  1. Gate level — for many randomly-shaped columns, every value actually in the column passes the
     scalar/format gates built from that column.
  2. End-to-end — build the real index over random tables, then every real cell value must be
     found by an indexed search at its true table + column.
Plus selectivity sanity: impossible values really are pruned.
"""

from __future__ import annotations

import random
import string

from dvp.services.column_profile import (
    ColumnStats,
    IndexPolicy,
    classify_column,
    passes_meta_gates,
    value_features,
)

API = "/api/v1"


# ---------------- generators ----------------
def _gen_value(kind: str, rnd: random.Random) -> str:
    if kind == "intid":
        return str(rnd.randint(1, 10_000_000))
    if kind == "zip":
        return f"{rnd.randint(0, 99999):05d}"  # includes leading zeros
    if kind == "state":
        return rnd.choice(["NC", "NY", "CA", "TX", "SC", "WA", "FL"])
    if kind == "code":
        return f"{rnd.choice('ABCDE')}-{rnd.randint(0, 9999):04d}"
    if kind == "email":
        return f"{_rand_word(rnd, 3, 8)}@{_rand_word(rnd, 3, 6)}.com"
    if kind == "decimal":
        return f"{rnd.randint(0, 9999)}.{rnd.randint(0, 99):02d}"
    if kind == "date":
        return f"20{rnd.randint(10, 26):02d}-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}"
    return _rand_word(rnd, 3, 20)  # text


def _rand_word(rnd: random.Random, lo: int, hi: int) -> str:
    return "".join(rnd.choice(string.ascii_lowercase) for _ in range(rnd.randint(lo, hi)))


KINDS = ["intid", "zip", "state", "code", "email", "decimal", "date", "text"]


# ---------------- 0. indexability policy ----------------
def test_classify_column_taxonomy():
    """The policy indexes identifiers/codes/enums and ignores binary, dates, oversized, and
    large high-cardinality text — the value×cost taxonomy."""
    p = IndexPolicy()  # long_len=100, high_card_ratio=0.5, hard_max_len=2000

    def idx(**kw):
        defaults = dict(data_type="VARCHAR", non_null=1000, distinct_est=1000, avg_len=10, max_len=12)
        return classify_column(policy=p, **{**defaults, "max_len": kw.get("max_len", kw.get("avg_len", 12)), **kw})

    # KEEP
    assert idx(data_type="NUMBER", avg_len=8, max_len=9, distinct_est=1000)[0]      # unique id
    assert idx(avg_len=6, max_len=8, distinct_est=5)[0]                              # low-card enum
    assert idx(avg_len=30, max_len=60, distinct_est=200)[0]                         # names (medium)
    assert idx(avg_len=210, max_len=230, distinct_est=3)[0]                         # long LOW-card boilerplate
    # IGNORE
    assert not idx(data_type="CLOB")[0]                                             # binary/LOB
    assert not idx(data_type="TIMESTAMP")[0]                                        # date/time
    assert not idx(non_null=0)[0]                                                   # empty
    assert not idx(avg_len=180, max_len=220, distinct_est=950)[0]                   # long HIGH-card free text
    assert not idx(avg_len=50, max_len=5000, distinct_est=10)[0]                    # oversized outlier value
    # the reason is human-readable
    assert "high-cardinality text" in classify_column(
        policy=p, data_type="VARCHAR", non_null=1000, distinct_est=950, avg_len=180, max_len=220
    )[1]


# ---------------- 1. gate-level property ----------------
def test_gates_never_reject_a_value_the_column_contains():
    rnd = random.Random(1234)
    for _ in range(300):
        kind = rnd.choice(KINDS)
        values = [_gen_value(kind, rnd) for _ in range(rnd.randint(1, 40))]
        stats = ColumnStats()
        for v in values:
            stats.add(v)
        meta = stats.finalize()
        for v in values:  # every present value MUST survive the gates
            assert passes_meta_gates(value_features(v), meta), (
                f"gate wrongly rejected {v!r} from a {kind} column meta={meta}"
            )


def test_gates_are_actually_selective():
    """Impossible values should be pruned — otherwise the gates buy us nothing."""
    # A pure 5-digit numeric (zip) column
    stats = ColumnStats()
    for z in ["27519", "10001", "90210", "60601"]:
        stats.add(z)
    meta = stats.finalize()
    assert passes_meta_gates(value_features("27519"), meta)       # present → keep
    assert not passes_meta_gates(value_features("NC"), meta)      # letters → charset/numeric prune
    assert not passes_meta_gates(value_features("275190"), meta)  # 6 digits → length prune
    assert not passes_meta_gates(value_features("abcde"), meta)   # wrong charset → prune

    # A 2-letter state column
    st = ColumnStats()
    for s in ["NC", "NY", "CA"]:
        st.add(s)
    meta2 = st.finalize()
    assert passes_meta_gates(value_features("NC"), meta2)
    assert not passes_meta_gates(value_features("27519"), meta2)  # digits vs letters
    assert not passes_meta_gates(value_features("NORTH"), meta2)  # length


# ---------------- 2. end-to-end property ----------------
def _conn(client, coords):
    return client.post(
        f"{API}/connections",
        json={"source_type": "local", "name": "c", "auth_method": "none", "coordinates": coords},
    ).json()["data"]["id"]


def _random_coords(rnd: random.Random) -> dict:
    tables = {}
    for t in range(rnd.randint(2, 4)):
        kinds = rnd.sample(KINDS, rnd.randint(2, 4))
        cols = [{"name": f"C{i}_{k.upper()}", "data_type": "STRING"} for i, k in enumerate(kinds)]
        rows = []
        for _ in range(rnd.randint(3, 15)):
            rows.append({cols[i]["name"]: _gen_value(k, rnd) for i, k in enumerate(kinds)})
        tables[f"T{t}"] = {"columns": cols, "rows": rows}
    return {"database": "RAW", "schema": "PUBLIC", "tables": tables}


def test_indexed_search_finds_every_real_value(client):
    rnd = random.Random(42)
    coords = _random_coords(rnd)
    cid = _conn(client, coords)
    built = client.post(f"{API}/finder/index:build", json={"connection_id": cid})
    assert built.status_code == 200, built.text

    # For every real cell, an indexed search must locate it at its true table + column.
    for tname, spec in coords["tables"].items():
        for row in spec["rows"]:
            for col, val in row.items():
                data = client.post(
                    f"{API}/finder/search",
                    json={"connection_id": cid, "values": [str(val)], "schema": "PUBLIC"},
                ).json()["data"]
                assert data["used_index"] is True
                located = {
                    (h["table"], c["column"])
                    for h in data["hits"]
                    for c in h["columns"]
                }
                assert (tname, col) in located, (
                    f"indexed search lost {val!r} expected in {tname}.{col}; got {sorted(located)}"
                )


def test_indexed_equals_live_scan_on_random_data(client):
    """The index must return exactly what a brute-force live scan would — same hits, no misses."""
    rnd = random.Random(7)
    coords = _random_coords(rnd)
    cid = _conn(client, coords)
    client.post(f"{API}/finder/index:build", json={"connection_id": cid})

    # pick a sample of real values + a few impossible ones
    sample = []
    for spec in coords["tables"].values():
        for row in spec["rows"][:2]:
            sample.extend(str(v) for v in row.values())
    sample += ["definitely-not-present-xyz", "999999999999"]

    def cols(val: str, use_index: str) -> set:
        d = client.post(
            f"{API}/finder/search",
            json={"connection_id": cid, "values": [val], "schema": "PUBLIC", "use_index": use_index},
        ).json()["data"]
        return {(h["table"], c["column"]) for h in d["hits"] for c in h["columns"]}

    for val in sample:
        assert cols(val, "auto") == cols(val, "never"), f"index/live disagree for {val!r}"
