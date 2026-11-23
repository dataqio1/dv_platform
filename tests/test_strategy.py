from dvp.testing_engine.strategy import CompareInputs, CompareStrategy, choose_strategy


def _inp(**kw):
    base = dict(
        same_snowflake_account=False,
        same_dialect=False,
        dialect_supports_pushdown=False,
        key_is_integer=False,
        has_transform_or_tolerance_or_filter=False,
    )
    base.update(kw)
    return CompareInputs(**base)


def test_same_snowflake_account_uses_except_fastpath():
    assert choose_strategy(_inp(same_snowflake_account=True)) == CompareStrategy.SERVER_SIDE_EXCEPT


def test_same_dialect_uses_pushdown():
    s = choose_strategy(_inp(same_dialect=True, dialect_supports_pushdown=True))
    assert s == CompareStrategy.PUSHDOWN_EXCEPT_JOIN


def test_integer_key_uses_adaptive():
    assert choose_strategy(_inp(key_is_integer=True)) == CompareStrategy.SPARK_ADAPTIVE_RANGE


def test_cross_db_fallback_is_hash_greedy():
    # Oracle <-> Snowflake, non-integer key -> general fallback
    assert choose_strategy(_inp()) == CompareStrategy.SPARK_HASH_GREEDY


def test_transform_disables_fastpaths():
    s = choose_strategy(_inp(same_snowflake_account=True, has_transform_or_tolerance_or_filter=True))
    assert s != CompareStrategy.SERVER_SIDE_EXCEPT
