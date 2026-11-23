"""Name-independent column mapping + key detection (ported SmartColumnMapper logic)."""

from __future__ import annotations

from dvp.testing_engine.column_mapping import (
    detect_minimal_key,
    jaccard,
    map_columns,
    map_keys,
)


def test_exact_and_prefix_and_abbrev():
    src = ["ORDER_ID", "c_custkey", "CUSTOMER_NAME", "QTY"]
    tgt = ["order_id", "custkey", "cust_name", "quantity"]
    m = map_columns(src, tgt)
    assert m.mappings["ORDER_ID"] == "order_id" and m.method["ORDER_ID"] == "EXACT_MATCH"
    assert m.mappings["c_custkey"] == "custkey" and m.method["c_custkey"] == "PREFIX_MATCH"
    assert m.mappings["CUSTOMER_NAME"] == "cust_name"  # customer→cust, name==name
    assert m.method["CUSTOMER_NAME"] == "ABBREVIATION_MATCH"
    assert m.mappings["QTY"] == "quantity" and m.method["QTY"] == "ABBREVIATION_MATCH"


def test_confidence_ordering_and_unmapped():
    m = map_columns(["AMOUNT", "MYSTERY"], ["amt", "other"])
    assert m.mappings["AMOUNT"] == "amt"
    assert m.confidence["AMOUNT"] == 0.8  # abbreviation
    assert "MYSTERY" in m.unmapped_source
    assert "other" in m.unmapped_target


def test_user_provided_wins():
    m = map_columns(["A", "B"], ["X", "Y"], provided={"A": "Y"})
    assert m.mappings["A"] == "Y" and m.method["A"] == "USER_PROVIDED"
    assert m.confidence["A"] == 1.0


def test_data_similarity_fallback():
    # names give no hint; values do
    src = ["COL1"]
    tgt = ["ZZZ"]
    m = map_columns(
        src,
        tgt,
        src_samples={"COL1": {"a", "b", "c", "d"}},
        dst_samples={"ZZZ": {"a", "b", "c", "e"}},
    )
    assert m.mappings["COL1"] == "ZZZ" and m.method["COL1"] == "DATA_SIMILARITY"
    assert m.confidence["COL1"] > 0.3


def test_data_similarity_respects_floor():
    m = map_columns(
        ["COL1"],
        ["ZZZ"],
        src_samples={"COL1": {"a", "b"}},
        dst_samples={"ZZZ": {"y", "z"}},
    )
    assert "COL1" in m.unmapped_source  # no overlap → below floor


def test_jaccard():
    assert jaccard({1, 2, 3}, {2, 3, 4}) == 0.5
    assert jaccard(set(), {1}) == 0.0


def test_detect_minimal_key_single():
    rows = [{"ID": i, "GRP": i % 2} for i in range(10)]
    key, ratio = detect_minimal_key(["ID", "GRP"], rows)
    assert key == ["ID"] and ratio == 1.0


def test_detect_minimal_key_composite():
    # neither column unique alone; the pair is
    rows = [{"A": a, "B": b} for a in range(3) for b in range(3)]
    key, ratio = detect_minimal_key(["A", "B"], rows)
    assert set(key) == {"A", "B"} and ratio == 1.0


def test_map_keys_across_different_names():
    # source key maps to a differently-named target key
    mapping = map_keys(["c_custkey"], ["custkey", "name", "addr"])
    assert mapping["c_custkey"] == "custkey"


def test_map_keys_positional_fallback():
    mapping = map_keys(["WIDGET_SK"], ["surrogate_key"])
    assert mapping["WIDGET_SK"] == "surrogate_key"  # positional when nothing else fits
