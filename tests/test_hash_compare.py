"""Snowflake hash-pushdown compare — classification/bucketing parity with the pure engine.

Live Snowflake SQL (HASH, row-constructor IN) is exercised by the env-gated functional
script scripts/test_hash_compare.sh. Here we verify the engine's Python logic: given the
compact per-key aggregates Snowflake would return, it buckets exactly like `compare_rows`.
The in-test connector computes those aggregates from the same rows, mirroring the SQL
normalization — so equal rows hash equal — and answers phase-2 fetches with the real rows.
"""

from __future__ import annotations

from dvp.testing_engine.rowcompare import CompareOptions, compare_rows
from dvp.testing_engine.snowflake_hash import compare_hash, supported
from dvp.testing_engine.typemap import CanonicalType, canonicalize


class _Res:
    def __init__(self, columns, rows):
        self.columns = columns
        self.rows = rows


def _norm_py(v, canon, opts):
    if v is None:
        return None
    if canon == CanonicalType.NUMERIC:
        return float(v)
    s = str(v)
    if opts.ignore_spaces:
        s = s.strip()
    if opts.case_insensitive:
        s = s.lower()
    if opts.null_equals_empty and s == "":
        return None
    return s


class _FakeSnowflake:
    """Emulates a Snowflake connector: the GROUP BY aggregate query returns per-key
    (count, min-hash, max-hash); any other SELECT returns the real rows (superset is fine
    — phase-2 filters by key downstream)."""

    dialect = "snowflake"

    def __init__(self, rows, key, columns, types, opts):
        self.rows = rows
        self.key = key
        self.columns = columns
        self.types = types
        self.opts = opts

    def execute(self, sql):
        if "GROUP BY" in sql:
            groups: dict[tuple, list] = {}
            for r in self.rows:
                k = tuple(r.get(c) for c in self.key)
                h = tuple(
                    _norm_py(r.get(c), canonicalize(self.types.get(c, "string")), self.opts)
                    for c in self.columns
                )
                groups.setdefault(k, []).append(hash(h))
            cols = [*self.key, "__CNT", "__MN", "__MX"]
            out = [[*k, len(hs), min(hs), max(hs)] for k, hs in groups.items()]
            return _Res(cols, out)
        all_cols = [*self.key, *self.columns]
        return _Res(all_cols, [[r.get(c) for c in all_cols] for r in self.rows])


# ID1 match · ID2 AMOUNT differs · ID3 source-only · ID4 target-only · ID5 ambiguous (dup src)
SRC = [
    {"ID": 1, "AMOUNT": 10, "NOTE": "ok"},
    {"ID": 2, "AMOUNT": 20, "NOTE": "ok"},
    {"ID": 3, "AMOUNT": 30, "NOTE": "ok"},
    {"ID": 5, "AMOUNT": 50, "NOTE": "a"},
    {"ID": 5, "AMOUNT": 55, "NOTE": "b"},
]
DST = [
    {"ID": 1, "AMOUNT": 10, "NOTE": "ok"},
    {"ID": 2, "AMOUNT": 25, "NOTE": "ok"},
    {"ID": 4, "AMOUNT": 40, "NOTE": "ok"},
    {"ID": 5, "AMOUNT": 50, "NOTE": "a"},
]
KEY, COLS = ["ID"], ["AMOUNT", "NOTE"]
TYPES = {"AMOUNT": "NUMBER", "NOTE": "VARCHAR"}


def test_supported_gate():
    opts = CompareOptions()
    assert supported(
        _FakeSnowflake([], KEY, COLS, TYPES, opts), _FakeSnowflake([], KEY, COLS, TYPES, opts), opts
    )
    # tolerance defeats exact hashing
    assert not supported(
        _FakeSnowflake([], KEY, COLS, TYPES, opts),
        _FakeSnowflake([], KEY, COLS, TYPES, opts),
        CompareOptions(tolerance=1.0),
    )


def test_hash_buckets_match_rowcompare():
    opts = CompareOptions()
    s = _FakeSnowflake(SRC, KEY, COLS, TYPES, opts)
    d = _FakeSnowflake(DST, KEY, COLS, TYPES, opts)
    outcome, notes = compare_hash(
        s,
        d,
        src_from="SRC",
        dst_from="DST",
        src_predicate=None,
        dst_predicate=None,
        key=KEY,
        columns=COLS,
        column_types=TYPES,
        options=opts,
    )
    mem = compare_rows(SRC, DST, key=KEY, columns=COLS, column_types=TYPES, options=opts)

    for f in (
        "matched_rows",
        "mismatched_rows",
        "src_only_rows",
        "dst_only_rows",
        "ambiguous_rows",
        "keys_total",
        "src_row_count",
        "dst_row_count",
    ):
        assert getattr(outcome.summary, f) == getattr(mem.summary, f), f

    buckets = {d.bucket for d in outcome.differences}
    assert {"value_difference", "source_only", "target_only", "ambiguous"} <= buckets

    vd = next(x for x in outcome.differences if x.bucket == "value_difference")
    assert vd.key == {"ID": 2}
    by = {c.column: c for c in vd.cells}
    assert by["AMOUNT"].differs is True and by["NOTE"].differs is False  # all columns present

    amb = next(x for x in outcome.differences if x.bucket == "ambiguous")
    assert amb.key == {"ID": 5} and amb.src_count == 2 and amb.dst_count == 1
    assert any("sampled differing rows" in n for n in notes)
