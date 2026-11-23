"""Stage-0 sample gate for cross-engine compares.

Before trusting a hash/fingerprint compare on a whole table, prove the encoding holds on a small
STRATIFIED sample compared IN MEMORY with the authoritative comparator (``compare_rows`` /
``values_equal`` — type-aware, so it already treats 1.5 == 1.5E0 and date == date). This cheaply:

  * catches a column that differs ~100% of the time (a shifted date, a timezone, a trimmed string)
    — a *systematic* difference we flag so the UI can mark it red, instead of letting it dirty every
    hash bucket and collapse into a useless "everything differs";
  * decides the route: PROCEED with the hash, go straight to STREAM (sample already shows heavy
    divergence — hashing can't prune), or INCONCLUSIVE (too few shared keys — let the hash's own gate
    decide).

~120 rows, two queries per side. Correctness is never at stake: the full engines remain the judge;
this only STEERS which engine runs and surfaces per-column diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dvp.testing_engine.rowcompare import CompareOptions, compare_rows

SYSTEMATIC_MATCH_CEILING = 0.05   # a column matching <5% of common sampled rows is "systematic"
MIN_COMMON = 20                   # need this many shared keys before we trust the sample
HEAVY_DIVERGENCE = 0.80           # >80% of common rows differ ⇒ skip hashing, stream instead
ENCODING_VALUES_FLOOR = 0.95      # column whose VALUES mostly match…
ENCODING_CANON_CEILING = 0.95     # …but whose cross-engine HASH text mostly disagrees ⇒ hash-unreliable


@dataclass
class GateResult:
    decision: str                                   # "proceed" | "stream" | "inconclusive"
    sampled: int = 0
    common: int = 0
    value_mismatch_rows: int = 0
    column_match_rate: dict[str, float] = field(default_factory=dict)
    systematic_columns: list[str] = field(default_factory=list)  # ~100%-different columns (flag red)
    encoding_suspect_columns: list[str] = field(default_factory=list)  # values match, hash text disagrees
    note: str = ""


def _q(c: str) -> str:
    return '"' + c.replace('"', '""') + '"'


def _lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _fetch_by_keys(pull, conn, from_sql, pred, cols, key_cols, raw_keys):  # noqa: ANN001
    """Fetch rows whose key TUPLE is in ``raw_keys`` (supports 1–6 key columns), via a row-tuple
    IN-list — the same form snowflake_hash uses, which Oracle and Snowflake both accept."""
    if not raw_keys:
        return []
    kc = ", ".join(_q(k) for k in key_cols)
    tuples = ", ".join("(" + ", ".join(_lit(v) for v in kt) + ")" for kt in raw_keys)
    kpred = f"({kc}) IN ({tuples})"
    full = f"({pred}) AND {kpred}" if pred else kpred
    return pull(conn, from_sql, cols, full)


def _knorm(kt: tuple) -> tuple:
    """Normalize a key tuple for cross-engine alignment (int 1 / Decimal('1') / '1' align)."""
    return tuple(None if v is None else str(v) for v in kt)


def _canonical_by_key(conn, from_sql, pred, dialect, key_cols, columns, types, opts, raw_keys):  # noqa: ANN001
    """{normalized key tuple: [canonical text per column]} for the sample keys — using the SAME
    cross-engine canonical the fingerprint hashes. Lets us check whether the HASH would agree on
    rows the authoritative comparator calls equal (it should; if not, the hash is unreliable here)."""
    from dvp.testing_engine.fingerprint_compare import _xnorm
    from dvp.testing_engine.typemap import canonicalize

    nk = len(key_cols)
    ksel = ", ".join(_q(k) for k in key_cols)
    csel = ", ".join(
        f"{_xnorm(dialect, c, canonicalize(types.get(c, 'string')), opts)} AS C{i}"
        for i, c in enumerate(columns)
    )
    tuples = ", ".join("(" + ", ".join(_lit(v) for v in kt) + ")" for kt in raw_keys)
    sql = f"SELECT {ksel}, {csel} FROM {from_sql} WHERE ({ksel}) IN ({tuples})"
    if pred:
        sql += f" AND ({pred})"
    res = conn.execute(sql)
    out: dict[tuple, list] = {}
    for row in res.rows:
        out[_knorm(tuple(row[:nk]))] = [row[nk + i] for i in range(len(columns))]
    return out


def precheck(
    pull,  # noqa: ANN001 — service._pull(connector, from_sql, cols, predicate=None, limit=None, order_by=None)
    src_conn: Any,
    dst_conn: Any,
    *,
    src_from: str,
    dst_from: str,
    src_predicate: str | None,
    dst_predicate: str | None,
    key: list[str],
    columns: list[str],
    src_cols: list[str],
    dst_cols: list[str],
    column_mappings: dict | None = None,
    column_types: dict[str, str] | None = None,
    options: CompareOptions | None = None,
    sample_size: int = 120,
    log: Any = None,
) -> GateResult:
    opts = options or CompareOptions()
    _log = log or (lambda *_a, **_k: None)
    if not (1 <= len(key) <= 6):  # single or composite key (up to 6 columns)
        return GateResult("inconclusive", note="sample gate needs 1–6 key columns")
    half = max(10, sample_size // 2)

    def ktuple(r: dict) -> tuple:
        return tuple(r.get(k) for k in key)

    # Stratified draw: head + tail ordered by the FULL key, so a systematic (table-wide) issue is
    # caught wherever it lives in the key space. ORDER BY spans all key columns (composite-safe).
    asc = ", ".join(f"{_q(k)} ASC" for k in key)
    desc = ", ".join(f"{_q(k)} DESC" for k in key)
    try:
        head = pull(src_conn, src_from, src_cols, src_predicate, limit=half, order_by=asc)
        tail = pull(src_conn, src_from, src_cols, src_predicate, limit=half, order_by=desc)
    except Exception as exc:  # noqa: BLE001 — sampling unsupported ⇒ let the hash gate decide
        return GateResult("inconclusive", note=f"sampling failed ({exc})")
    dedup: dict[tuple, dict] = {}
    for r in head + tail:
        dedup.setdefault(ktuple(r), r)
    src_rows = list(dedup.values())
    raw_keys = list(dedup.keys())
    if not raw_keys:
        return GateResult("inconclusive", sampled=0, note="source sample empty")

    dst_key_cols = [(column_mappings or {}).get(k, k) for k in key] if column_mappings else list(key)
    dst_raw = _fetch_by_keys(pull, dst_conn, dst_from, dst_predicate, dst_cols, dst_key_cols, raw_keys)
    # rename target columns → source names so compare_rows lines them up
    if column_mappings:
        rename = dict(zip(dst_cols, src_cols, strict=False))
        dst_rows = [{rename.get(k2, k2): v for k2, v in r.items()} for r in dst_raw]
    else:
        dst_rows = dst_raw

    out = compare_rows(
        src_rows, dst_rows, key=key, columns=columns,
        column_types=column_types or {}, options=opts,
    )
    s = out.summary
    common = s.matched_rows + s.mismatched_rows  # rows present on BOTH sides (the ones we can judge)
    sampled = len(src_rows)
    if common < MIN_COMMON:
        return GateResult(
            "inconclusive", sampled=sampled, common=common,
            note=f"only {common} shared keys in the sample — too few to judge; deferring to the hash gate",
        )

    # Per-column match rate on the common rows; a near-zero rate ⇒ systematic difference.
    rate = {
        c: round((common - s.per_column_mismatch.get(c, 0)) / common, 4)
        for c in columns
    }
    systematic = sorted(c for c in columns if rate[c] <= SYSTEMATIC_MATCH_CEILING)
    value_mismatch_pct = s.mismatched_rows / common if common else 0.0

    # ---- encoding check: do the HASH canonicals agree on rows the VALUES call equal? A column whose
    # values mostly MATCH but whose cross-engine canonical text mostly DISAGREES is a canonical-contract
    # gap — the hash would wrongly flag it and over-drill. We can't just drop it (real diffs would be
    # missed), so route to the authoritative streaming engine up front (amber, not red: data matches).
    encoding_suspect: list[str] = []
    try:
        dst_columns = [(column_mappings or {}).get(c, c) for c in columns]
        dst_types = {d: (column_types or {}).get(sc, "string") for sc, d in zip(columns, dst_columns)}
        scan = _canonical_by_key(src_conn, src_from, src_predicate, getattr(src_conn, "dialect", ""),
                                 key, columns, column_types or {}, opts, raw_keys)
        dcan = _canonical_by_key(dst_conn, dst_from, dst_predicate, getattr(dst_conn, "dialect", ""),
                                 dst_key_cols, dst_columns, dst_types, opts, raw_keys)
        common_keys = set(scan) & set(dcan)
        if len(common_keys) >= MIN_COMMON:
            for i, c in enumerate(columns):
                if c in systematic:
                    continue  # already a real diff; not an encoding artifact
                agree = sum(1 for k in common_keys if str(scan[k][i]) == str(dcan[k][i]))
                if rate[c] >= ENCODING_VALUES_FLOOR and (agree / len(common_keys)) < ENCODING_CANON_CEILING:
                    encoding_suspect.append(c)
    except Exception:  # noqa: BLE001 — best-effort; absence just means we rely on the hash's own bail
        encoding_suspect = []

    if systematic and len(systematic) == len(columns):
        decision = "stream"
        note = f"every compared column differs in the sample — hashing can't prune; streaming instead"
    elif value_mismatch_pct > HEAVY_DIVERGENCE:
        decision = "stream"
        note = f"{value_mismatch_pct:.0%} of sampled rows differ — too divergent for hashing; streaming instead"
    elif encoding_suspect:
        decision = "stream"
        note = (f"hash encoding unreliable for {', '.join(encoding_suspect)} (values match but the "
                f"cross-engine canonical disagrees) — using the authoritative row compare")
    else:
        decision = "proceed"
        if systematic:
            note = f"sample clean enough to hash; systematic difference in: {', '.join(systematic)}"
        else:
            note = "sample matches — proceeding with hash compare"

    _log(
        f"Sample gate: {sampled} sampled, {common} common, {s.mismatched_rows} value-diff "
        f"({value_mismatch_pct:.0%})"
        + (f"; systematic: {', '.join(systematic)}" if systematic else "")
        + (f"; hash-unreliable: {', '.join(encoding_suspect)}" if encoding_suspect else "")
        + f" → {decision}"
    )
    return GateResult(
        decision=decision, sampled=sampled, common=common,
        value_mismatch_rows=s.mismatched_rows, column_match_rate=rate,
        systematic_columns=systematic, encoding_suspect_columns=encoding_suspect, note=note,
    )
