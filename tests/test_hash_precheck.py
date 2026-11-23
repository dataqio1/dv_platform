"""Sample pre-check tripwire for the Snowflake hash compare (no mocks — a Snowflake-shaped
fake connector answers SAMPLE / IN / COUNT queries from in-memory rows).

The guard short-circuits ONLY on heavy VALUE divergence among keys present in BOTH sides. Rows
missing from the target (source-only) are cheap for the full compare and never trip it, and if the
sample can't find enough common keys we don't trust it — the full compare runs instead.
"""
from __future__ import annotations

from dvp.testing_engine.rowcompare import CompareOptions
from dvp.testing_engine.snowflake_hash import (
    outcome_from_precheck,
    precheck_short_circuit,
    sample_precheck,
)


class _Res:
    def __init__(self, columns, rows):
        self.columns = columns
        self.rows = rows


class _Fake:
    """Answers COUNT with its row count, and SAMPLE / IN both with ALL its rows (the real key
    filtering happens in _fetch_rows_by_key via the returned key values, so source-only keys from
    the other side resolve to None exactly as on Snowflake)."""

    dialect = "snowflake"

    def __init__(self, rows, key, columns):
        self.rows = rows
        self.key = key
        self.columns = columns

    def execute(self, sql):
        if "COUNT(*)" in sql.upper():
            return _Res(["__C"], [[len(self.rows)]])
        cols = [*self.key, *self.columns]
        return _Res(cols, [[r.get(c) for c in cols] for r in self.rows])


KEY, COLS, TYPES = ["ID"], ["AMOUNT"], {"AMOUNT": "NUMBER"}


def _pc(src, dst, **kw):
    kw.setdefault("sample_size", 20)  # fake returns all rows in one round
    kw.setdefault("rounds", 1)
    kw.setdefault("min_common", 3)    # need ≥3 keys in both to judge values
    return sample_precheck(
        _Fake(src, KEY, COLS), _Fake(dst, KEY, COLS),
        src_from="S", dst_from="D", src_predicate=None, dst_predicate=None,
        key=KEY, columns=COLS, column_types=TYPES, options=CompareOptions(), **kw,
    )


# ── the verdict function (pure, the heart of the fix) ──
def test_verdict_ignores_missing_rows_and_needs_enough_common():
    # heavy value divergence among plenty of common keys → short-circuit
    assert precheck_short_circuit(common=10, value_mismatched=9, min_common=3, max_mismatch_pct=20) is True
    # same divergence but too few common keys to trust → do NOT short-circuit
    assert precheck_short_circuit(common=2, value_mismatched=2, min_common=3, max_mismatch_pct=20) is False
    # no common keys at all (sample never landed in the target) → never short-circuit
    assert precheck_short_circuit(common=0, value_mismatched=0, min_common=3, max_mismatch_pct=20) is False
    # common keys mostly match → proceed to the (now cheap) full compare
    assert precheck_short_circuit(common=10, value_mismatched=1, min_common=3, max_mismatch_pct=20) is False


def test_clean_sample_proceeds():
    rows = [{"ID": i, "AMOUNT": i * 10} for i in range(1, 6)]
    pc = _pc(rows, [dict(r) for r in rows])
    assert pc.common == 5 and pc.matched == 5 and pc.source_only == 0
    assert pc.short_circuit is False and pc.match_rate_pct == 100.0


def test_heavy_value_mismatch_short_circuits():
    src = [{"ID": i, "AMOUNT": i * 10} for i in range(1, 6)]
    dst = [{"ID": i, "AMOUNT": i * 10 + (1 if i > 1 else 0)} for i in range(1, 6)]  # 4/5 values differ
    pc = _pc(src, dst)
    assert pc.common == 5 and pc.source_only == 0
    assert pc.short_circuit is True  # 80% value mismatch > 20% threshold
    assert any(d.bucket == "value_difference" for d in pc.differences)
    out = outcome_from_precheck(pc)
    assert out.summary.mismatched_rows == 4 and out.summary.keys_total == 5


def test_source_only_rows_do_not_short_circuit():
    """THE fix: the sampled source keys are largely absent from the target. Those are missing
    rows, not value divergence — the guard must NOT trip; the full compare handles them cheaply."""
    src = [{"ID": i, "AMOUNT": i} for i in range(1, 21)]      # 20 rows
    dst = [{"ID": i, "AMOUNT": i} for i in range(1, 6)]       # only 5 present on the target
    pc = _pc(src, dst)
    assert pc.common == 5 and pc.source_only == 15
    assert pc.short_circuit is False                          # missing rows never trip it
    assert any(d.bucket == "source_only" for d in pc.differences)


def test_sample_with_no_common_keys_proceeds():
    """The user's case: the 200 sampled records aren't found in the target at all (disjoint key
    ranges). common == 0 → we don't trust the sample and run the full compare."""
    src = [{"ID": i, "AMOUNT": i} for i in range(1, 11)]
    dst = [{"ID": i, "AMOUNT": i} for i in range(100, 111)]   # no shared keys
    pc = _pc(src, dst)
    assert pc.common == 0 and pc.source_only == 10
    assert pc.short_circuit is False
    assert pc.inconclusive is True  # we tell the caller we couldn't judge
    assert "too few common rows" in pc.note


def test_few_value_mismatches_below_threshold_proceeds():
    src = [{"ID": i, "AMOUNT": i} for i in range(1, 21)]
    dst = [{"ID": i, "AMOUNT": i + (1 if i == 7 else 0)} for i in range(1, 21)]  # 1 differs = 5%
    pc = _pc(src, dst)
    assert pc.common == 20 and pc.source_only == 0
    assert pc.short_circuit is False  # 5% <= 20%
