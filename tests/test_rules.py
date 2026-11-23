from dvp.observability.rules import (
    NotNullRule,
    NullRateRule,
    RowCountRule,
    UniqueRule,
)


def test_not_null_rule():
    rule = NotNullRule(column="email")
    assert "nulls__email" in rule.to_sql("T", None)
    assert rule.evaluate({"nulls__email": 0}).passed
    assert not rule.evaluate({"nulls__email": 3}).passed


def test_null_rate_rule_threshold():
    rule = NullRateRule(column="email", max_rate=0.1)
    ok = rule.evaluate({"row_count": 100, "nulls__email": 5})
    bad = rule.evaluate({"row_count": 100, "nulls__email": 20})
    assert ok.passed and ok.metric_value == 0.05
    assert not bad.passed


def test_row_count_rule():
    assert RowCountRule(min_rows=1).evaluate({"row_count": 10}).passed
    assert not RowCountRule(min_rows=1).evaluate({"row_count": 0}).passed


def test_unique_rule_requires_full_scan():
    rule = UniqueRule(columns=["id"])
    assert rule.requires_full_scan is True  # the documented delta-only exception
    assert rule.evaluate({"total_rows_full": 100, "distinct_key_full": 100}).passed
    assert not rule.evaluate({"total_rows_full": 100, "distinct_key_full": 97}).passed
