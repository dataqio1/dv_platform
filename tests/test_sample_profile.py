"""Pure tests for sample profiling + per-rule sample evaluation (no DB, no warehouse)."""

from __future__ import annotations

from dataclasses import dataclass

from dvp.observability.autogen import generate_rules
from dvp.observability.sample_profile import build_profile, evaluate_rule_on_rows


@dataclass
class Col:
    name: str
    data_type: str


ROWS = [
    {"ID": 1, "EMAIL": "a@x.com", "AMT": 10, "NOTE": None},
    {"ID": 2, "EMAIL": "b@x.com", "AMT": 20, "NOTE": "hi"},
    {"ID": 3, "EMAIL": "c@x.com", "AMT": 30, "NOTE": None},
]
COLS = [Col("ID", "NUMBER"), Col("EMAIL", "VARCHAR"), Col("AMT", "NUMBER"), Col("NOTE", "VARCHAR")]


def test_build_profile_computes_stats():
    p = build_profile("T", COLS, ROWS)
    assert p.row_count == 3
    by = {c.name: c for c in p.columns}
    assert by["ID"].null_rate == 0.0 and by["ID"].distinct_ratio == 1.0 and by["ID"].is_key_candidate
    assert by["ID"].data_type == "NUMERIC"
    assert by["AMT"].min_value == 10 and by["AMT"].max_value == 30
    assert round(by["NOTE"].null_rate, 2) == 0.67  # 2 of 3 null
    assert by["EMAIL"].looks_like_email is True


def test_generated_rules_scale_with_level():
    p = build_profile("T", COLS, ROWS)
    l1 = {r["name"] for r in generate_rules(p, 1)}
    l2 = {r["name"] for r in generate_rules(p, 2)}
    l3 = {r["name"] for r in generate_rules(p, 3)}
    assert "not_null__ID" in l1 and "not_null__EMAIL" in l1
    assert l1 <= l2 <= l3                      # cumulative
    assert any(n.startswith("range__AMT") for n in l2)       # numeric range at L2
    assert any(n.startswith("null_rate__NOTE") for n in l2)  # partial-null → null_rate at L2
    assert any(n.startswith("regex_email__EMAIL") for n in l3)  # email regex at L3


def test_evaluate_rule_on_rows_pass_and_fail():
    # not_null passes on ID (no nulls), fails on NOTE (has nulls).
    assert evaluate_rule_on_rows({"type": "not_null", "columns": ["ID"]}, ROWS)["passed"] is True
    note = evaluate_rule_on_rows({"type": "not_null", "columns": ["NOTE"]}, ROWS)
    assert note["passed"] is False and note["measured"] == 2 and len(note["failing"]) == 2

    # range: AMT within [0,100] passes; within [0,25] fails on the 30 row.
    assert evaluate_rule_on_rows({"type": "range", "columns": ["AMT"], "config": {"min_value": 0, "max_value": 100}}, ROWS)["passed"] is True
    r = evaluate_rule_on_rows({"type": "range", "columns": ["AMT"], "config": {"min_value": 0, "max_value": 25}}, ROWS)
    assert r["passed"] is False and r["measured"] == 1

    # unique on ID passes; add a dup and it fails.
    assert evaluate_rule_on_rows({"type": "unique", "columns": ["ID"]}, ROWS)["passed"] is True
    dup = evaluate_rule_on_rows({"type": "unique", "columns": ["ID"]}, ROWS + [{"ID": 1}])
    assert dup["passed"] is False

    # freshness isn't evaluable on an in-memory sample.
    fr = evaluate_rule_on_rows({"type": "freshness", "columns": ["TS"]}, ROWS)
    assert fr["applicable"] is False
