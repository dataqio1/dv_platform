from dvp.testing_engine.keydetect import ColumnMeta, detect_key, is_keyable, rank_candidates


def test_non_keyable_types_excluded():
    assert not is_keyable(ColumnMeta("amt", "FLOAT"))
    assert not is_keyable(ColumnMeta("ts", "TIMESTAMP"))
    assert is_keyable(ColumnMeta("id", "NUMBER(38,0)"))
    assert is_keyable(ColumnMeta("code", "VARCHAR2(10)"))


def test_ranking_prioritizes_id_columns():
    cols = [
        ColumnMeta("name", "VARCHAR"),
        ColumnMeta("id", "NUMBER"),
        ColumnMeta("order_key", "NUMBER"),
    ]
    ranked = [c.name for c in rank_candidates(cols)]
    assert ranked[0] == "id"
    assert "order_key" in ranked[:2]


def test_detect_single_column_key():
    cols = [ColumnMeta("id", "NUMBER"), ColumnMeta("name", "VARCHAR")]

    def sample_ratio(c):
        return 1.0 if c == ["id"] else 0.5

    def validate(c):
        return c == ["id"]

    assert detect_key(cols, sample_distinct_ratio=sample_ratio, validate_unique_full=validate) == ["id"]


def test_detect_composite_key():
    cols = [ColumnMeta("region", "VARCHAR"), ColumnMeta("order_id", "NUMBER")]

    def sample_ratio(c):
        return 1.0 if set(c) == {"region", "order_id"} else 0.4

    def validate(c):
        return set(c) == {"region", "order_id"}

    result = detect_key(cols, sample_distinct_ratio=sample_ratio, validate_unique_full=validate)
    assert set(result) == {"region", "order_id"}
