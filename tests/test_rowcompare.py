from dvp.testing_engine.rowcompare import compare_rows

SRC = [
    {"ID": 1, "AMOUNT": "100.00", "STATUS": "OPEN"},
    {"ID": 2, "AMOUNT": "50.00", "STATUS": "CLOSED"},
    {"ID": 3, "AMOUNT": "75.00", "STATUS": "OPEN"},
]
DST = [
    {"ID": 1, "AMOUNT": "100.00", "STATUS": "OPEN"},  # exact match
    {"ID": 2, "AMOUNT": "50.50", "STATUS": "CLOSED"},  # AMOUNT differs
    {"ID": 4, "AMOUNT": "10.00", "STATUS": "OPEN"},  # dst only
]
TYPES = {"AMOUNT": "NUMBER", "STATUS": "VARCHAR"}


def test_cell_level_diffs():
    out = compare_rows(SRC, DST, key=["ID"], columns=["AMOUNT", "STATUS"], column_types=TYPES)
    s = out.summary
    assert s.matched_rows == 1  # ID 1 (pass)
    assert s.mismatched_rows == 1  # ID 2 (bucket 1: value_difference)
    assert s.src_only_rows == 1  # ID 3 (bucket 2)
    assert s.dst_only_rows == 1  # ID 4 (bucket 3)
    assert s.ambiguous_rows == 0  # bucket 4
    assert s.per_column_mismatch == {"AMOUNT": 1}

    mismatch = next(d for d in out.differences if d.bucket == "value_difference")
    assert mismatch.key == {"ID": 2}
    assert mismatch.cells[0].column == "AMOUNT"
    assert mismatch.cells[0].src == "50.00"
    assert mismatch.cells[0].dst == "50.50"

    # bucket 2/3 carry the actual record
    so = next(d for d in out.differences if d.bucket == "source_only")
    assert so.key == {"ID": 3} and so.record["AMOUNT"] == "75.00"
    to = next(d for d in out.differences if d.bucket == "target_only")
    assert to.key == {"ID": 4} and to.record["AMOUNT"] == "10.00"


def test_identical_data_matches_fully():
    out = compare_rows(SRC, SRC, key=["ID"], columns=["AMOUNT", "STATUS"], column_types=TYPES)
    assert out.summary.mismatched_rows == 0
    assert out.summary.match_rate_pct == 100.0
    assert out.differences == []
    # pass records are sampled so the UI can show good examples
    assert len(out.passed_sample) == 3
    assert out.passed_sample[0].bucket == "match"


def test_ambiguous_bucket_for_duplicate_keys():
    src = [{"ID": 1, "AMOUNT": "10", "STATUS": "A"}, {"ID": 1, "AMOUNT": "11", "STATUS": "A"}]
    dst = [{"ID": 1, "AMOUNT": "10", "STATUS": "A"}]
    out = compare_rows(src, dst, key=["ID"], columns=["AMOUNT", "STATUS"], column_types=TYPES)
    assert out.summary.ambiguous_rows == 1  # bucket 4: 2 source rows vs 1 target for key 1
    amb = next(d for d in out.differences if d.bucket == "ambiguous")
    assert amb.src_count == 2 and amb.dst_count == 1
