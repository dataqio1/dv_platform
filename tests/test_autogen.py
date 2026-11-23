from dvp.observability.autogen import ColumnProfile, TableProfile, generate_rules


def _profile():
    return TableProfile(
        table="ORDERS",
        row_count=1000,
        load_ts_column="LOADED_AT",
        detected_key=["ORDER_ID"],
        columns=[
            ColumnProfile(name="ORDER_ID", data_type="NUMERIC", null_rate=0.0, is_key_candidate=True),
            ColumnProfile(name="EMAIL", data_type="STRING", null_rate=0.03, looks_like_email=True),
            ColumnProfile(name="AMOUNT", data_type="NUMERIC", null_rate=0.0, min_value=0, max_value=5000),
        ],
    )


def test_levels_are_cumulative():
    p = _profile()
    l1 = {r["name"] for r in generate_rules(p, 1)}
    l2 = {r["name"] for r in generate_rules(p, 2)}
    l3 = {r["name"] for r in generate_rules(p, 3)}
    assert l1 <= l2 <= l3
    assert len(l3) > len(l2) > len(l1)


def test_level1_is_essential_only():
    names = {r["name"] for r in generate_rules(_profile(), 1)}
    assert "row_count_positive" in names
    assert "not_null__ORDER_ID" in names  # fully-populated column
    assert "freshness__LOADED_AT" in names
    assert not any(n.startswith("range__") for n in names)  # that's level 2


def test_level2_adds_unique_and_range():
    rules = generate_rules(_profile(), 2)
    names = {r["name"] for r in rules}
    assert "unique__key" in names
    assert "range__AMOUNT" in names
    # null_rate threshold is data-derived (observed 0.03 + margin)
    nr = next(r for r in rules if r["name"] == "null_rate__EMAIL")
    assert 0.03 < nr["config"]["max_rate"] <= 0.06


def test_level3_adds_email_regex():
    names = {r["name"] for r in generate_rules(_profile(), 3)}
    assert "regex_email__EMAIL" in names


def test_generated_rules_are_suggested_with_confidence():
    for r in generate_rules(_profile(), 3):
        assert r["state"] == "suggested"
        assert r["confidence"] is not None
