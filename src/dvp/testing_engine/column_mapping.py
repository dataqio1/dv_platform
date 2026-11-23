"""Name-independent column mapping + key detection.

Ported from dms_sample's SmartColumnMapper / KeyColumnMapper / UniqueKeyColumnDetector
(battle-tested over years) so a source table can be compared to a target even when the
column names differ (ETL renames, warehouse conventions, abbreviations).

Column mapping is a layered, first-match-wins cascade, each layer more expensive and less
certain than the last — so confident structural matches win before we ever look at data:

  1. USER_PROVIDED   (1.00) — explicit src→tgt the caller passed
  2. EXACT_MATCH     (1.00) — case-insensitive name equality
  3. PREFIX_MATCH    (0.90) — after stripping common prefixes (c_, src_, dim_, stg_, …)
  4. ABBREVIATION    (0.80) — tokenwise, each token equal or a known abbreviation
  5. SUFFIX_MATCH    (0.75) — containment / shared significant suffix
  6. DATA_SIMILARITY (≥0.30) — Jaccard overlap of sampled DISTINCT values (fallback)

Key detection mirrors KeyColumnMapper's cascade: caller-provided → (DB PK/unique index,
supplied by the connector layer) → data-driven minimal unique key (distinctness-ranked,
greedily extended to a composite until unique). Keys are then mapped across sides with the
same column mapper, so `ORDER_ID`↔`o_orderkey` lines up automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

COMMON_PREFIXES = [
    "c_",
    "t_",
    "d_",
    "f_",
    "src_",
    "tgt_",
    "source_",
    "target_",
    "dim_",
    "fact_",
    "stg_",
    "raw_",
    "tmp_",
    "temp_",
]

# token → known abbreviations/expansions (bidirectional pairs, as in dms_sample)
ABBREVIATION_MAP: dict[str, list[str]] = {
    "customer": ["cust", "cus", "cstmr"],
    "cust": ["customer", "cus", "cstmr"],
    "order": ["ord", "ordr"],
    "ord": ["order", "ordr"],
    "product": ["prod", "prd"],
    "prod": ["product", "prd"],
    "quantity": ["qty", "qnty"],
    "qty": ["quantity", "qnty"],
    "number": ["num", "no", "nbr"],
    "num": ["number", "no", "nbr"],
    "identifier": ["id", "ident"],
    "id": ["identifier", "ident", "key"],
    "description": ["desc", "descr"],
    "desc": ["description", "descr"],
    "address": ["addr", "add"],
    "addr": ["address", "add"],
    "telephone": ["tel", "phone", "ph"],
    "phone": ["telephone", "tel", "ph"],
    "amount": ["amt", "amnt"],
    "amt": ["amount", "amnt"],
    "date": ["dt", "dte"],
    "dt": ["date", "dte"],
    "time": ["tm", "tme"],
    "timestamp": ["ts", "tstamp"],
    "ts": ["timestamp", "tstamp"],
    "status": ["stat", "sts"],
    "stat": ["status", "sts"],
    "type": ["typ", "tp"],
    "code": ["cd", "cod"],
    "cd": ["code", "cod"],
    "name": ["nm", "nme"],
    "nm": ["name", "nme"],
    "first": ["fst", "frst"],
    "last": ["lst"],
    "middle": ["mid", "mdl"],
    "country": ["ctry", "cntry"],
    "region": ["rgn", "reg"],
    "nation": ["natn", "nat"],
    "price": ["prc", "pr"],
    "discount": ["disc", "dscnt"],
    "extended": ["ext", "extd"],
    "return": ["ret", "rtn"],
    "comment": ["cmt", "cmnt"],
    "key": ["ky", "k", "id"],
}


@dataclass
class ColumnMapping:
    mappings: dict[str, str] = field(default_factory=dict)  # source → target
    method: dict[str, str] = field(default_factory=dict)  # source → method
    confidence: dict[str, float] = field(default_factory=dict)  # source → 0..1
    unmapped_source: list[str] = field(default_factory=list)
    unmapped_target: list[str] = field(default_factory=list)

    def add(self, src: str, tgt: str, method: str, conf: float) -> None:
        self.mappings[src] = tgt
        self.method[src] = method
        self.confidence[src] = conf


def _remove_prefix(name: str) -> str:
    low = name.lower()
    for p in COMMON_PREFIXES:
        if low.startswith(p):
            return low[len(p) :]
    return low


def _split_name(name: str) -> list[str]:
    parts: list[str] = []
    for chunk in name.split("_"):
        for camel in re.split(r"(?=[A-Z])", chunk):
            if camel:
                parts.append(camel.lower())
    return parts


def _matches_abbrev(a: str, b: str) -> bool:
    pa, pb = _split_name(a.lower()), _split_name(b.lower())
    if len(pa) != len(pb):
        return False
    for x, y in zip(pa, pb, strict=False):
        if x == y:
            continue
        if y not in ABBREVIATION_MAP.get(x, []) and x not in ABBREVIATION_MAP.get(y, []):
            return False
    return True


def _matches_suffix(a: str, b: str) -> bool:
    n1 = re.sub(r"[_\s]", "", a.lower())
    n2 = re.sub(r"[_\s]", "", b.lower())
    if n1 in n2 or n2 in n1:
        shorter, longer = sorted((n1, n2), key=len)
        return longer.endswith(shorter) or len(shorter) >= len(longer) * 0.6
    return False


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


# ─── column PROFILE (sample-invariant) ───────────────────────────────────────
# Value-overlap (Jaccard) only works when both sides sampled the SAME rows. For high-cardinality
# columns on big tables the samples rarely overlap, so overlap → 0 and the match is missed. A
# column's *profile* — its type, semantic shape (email/phone/uuid/date/number) and length/magnitude
# distribution — is stable across different samples, so it recovers those matches. (Mirrors the
# statistical/semantic components of dms_sample's MatchScoreComponents.)

_RX = {
    "email": re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    "uuid": re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"),
    "date": re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}([ T]\d{1,2}:\d{2}.*)?$"),
    "decimal": re.compile(r"^-?\d+\.\d+$"),
    "integer": re.compile(r"^-?\d+$"),
    "phone": re.compile(r"^[+(]?[\d][\d\-\s().]{5,}$"),
}
# Patterns distinctive enough that matching ONE is a real signal (unlike generic "text").
_DISTINCTIVE = {"email", "uuid", "date", "decimal", "integer", "phone"}


def _detect_pattern(svals: list[str]) -> str:
    n = len(svals)
    for name in ("email", "uuid", "date", "decimal", "integer", "phone"):
        if sum(1 for v in svals if _RX[name].match(v)) >= 0.8 * n:
            return name
    return "text"


def profile(values: set) -> dict:
    """A sample-invariant fingerprint of a column from its sampled distinct values: type, semantic
    pattern, and length/magnitude stats."""
    svals = [str(v) for v in values if v is not None and str(v) != ""]
    if not svals:
        return {"kind": "empty"}
    nums: list[float] = []
    for v in svals:
        try:
            nums.append(float(v))
        except (ValueError, OverflowError):
            pass
    numeric = len(nums) >= 0.9 * len(svals)
    lens = [len(v) for v in svals]
    p = {
        "kind": "numeric" if numeric else "string",
        "pattern": _detect_pattern(svals),
        "len_avg": sum(lens) / len(lens),
    }
    if numeric and nums:
        p["num_mean"] = sum(nums) / len(nums)
        p["num_min"], p["num_max"] = min(nums), max(nums)
    return p


def profile_similarity(a: dict, b: dict) -> float:
    """0–1 similarity of two column profiles. Never crosses numeric↔string. Weighted like the
    reference's statistical+semantic components: semantic pattern dominates, then length, then
    numeric magnitude. Generic "text" gives no semantic credit (it isn't discriminative)."""
    if a.get("kind") in (None, "empty") or b.get("kind") in (None, "empty"):
        return 0.0
    if a["kind"] != b["kind"]:
        return 0.0
    score = 0.0
    if a["pattern"] == b["pattern"] and a["pattern"] in _DISTINCTIVE:
        score += 0.55  # same distinctive shape (email/phone/uuid/date/number) — the key signal
    la, lb = a["len_avg"], b["len_avg"]
    score += 0.25 * max(0.0, 1 - abs(la - lb) / max(la, lb, 1.0))  # length distribution
    if a["kind"] == "numeric":
        ma, mb = a.get("num_mean", 0.0), b.get("num_mean", 0.0)
        score += 0.20 * max(0.0, 1 - abs(ma - mb) / max(abs(ma), abs(mb), 1.0))  # magnitude
    elif a["pattern"] == b["pattern"] and a["pattern"] in _DISTINCTIVE:
        score += 0.20  # strings whose shape matches get the remaining credit
    return min(score, 1.0)


def map_columns(
    source_columns: list[str],
    target_columns: list[str],
    provided: dict[str, str] | None = None,
    *,
    src_samples: dict[str, set] | None = None,
    dst_samples: dict[str, set] | None = None,
    data_similarity_floor: float = 0.3,
    profile_floor: float = 0.55,
) -> ColumnMapping:
    """Map source → target column names via the layered cascade. `src_samples`/`dst_samples`
    (column → set of sampled DISTINCT string values) enable the value-Jaccard fallback and the
    sample-invariant profile fallback (`profile_floor`), which matches high-cardinality columns
    even when the two samples don't share rows."""
    res = ColumnMapping()
    rem_src = list(source_columns)
    rem_tgt = list(target_columns)

    def consume(src: str, tgt: str, method: str, conf: float) -> None:
        res.add(src, tgt, method, conf)
        rem_src.remove(src)
        rem_tgt.remove(tgt)

    # 1. user-provided
    if provided:
        for s, t in provided.items():
            if s in rem_src and t in rem_tgt:
                consume(s, t, "USER_PROVIDED", 1.0)

    # 2. exact (case-insensitive)
    for s in list(rem_src):
        for t in list(rem_tgt):
            if s.lower() == t.lower():
                consume(s, t, "EXACT_MATCH", 1.0)
                break

    # 3. prefix-strip
    for s in list(rem_src):
        sn = _remove_prefix(s)
        for t in list(rem_tgt):
            if sn == _remove_prefix(t):
                consume(s, t, "PREFIX_MATCH", 0.9)
                break

    # 4. abbreviation (tokenwise)
    for s in list(rem_src):
        for t in list(rem_tgt):
            if _matches_abbrev(s, t):
                consume(s, t, "ABBREVIATION_MATCH", 0.8)
                break

    # 5. suffix / containment
    for s in list(rem_src):
        for t in list(rem_tgt):
            if _matches_suffix(s, t):
                consume(s, t, "SUFFIX_MATCH", 0.75)
                break

    # 6a. data similarity — Jaccard over sampled distinct values. Strongest signal when both sides
    #     sampled overlapping rows. Assigned best-first so the most confident pairs claim first.
    if src_samples and dst_samples:
        overlaps = sorted(
            (
                (jaccard(src_samples.get(s, set()), dst_samples.get(t, set())), s, t)
                for s in rem_src
                for t in rem_tgt
            ),
            reverse=True,
            key=lambda x: x[0],
        )
        for score, s, t in overlaps:
            if score <= data_similarity_floor:
                break
            if s in rem_src and t in rem_tgt:
                consume(s, t, "DATA_SIMILARITY", round(score, 3))

        # 6b. profile / semantic match — for columns value-overlap couldn't resolve (e.g. the two
        #     tables sampled different rows, so a high-cardinality column never overlaps). Matches
        #     by type + semantic shape + length/magnitude, which are stable across samples. Lower
        #     confidence — surfaced for the user to confirm.
        src_prof = {s: profile(src_samples.get(s, set())) for s in rem_src}
        dst_prof = {t: profile(dst_samples.get(t, set())) for t in rem_tgt}
        prof_scores = sorted(
            ((profile_similarity(src_prof[s], dst_prof[t]), s, t) for s in rem_src for t in rem_tgt),
            reverse=True,
            key=lambda x: x[0],
        )
        for score, s, t in prof_scores:
            if score < profile_floor:
                break
            if s in rem_src and t in rem_tgt:
                consume(s, t, "PROFILE_MATCH", round(score, 3))

    res.unmapped_source = rem_src
    res.unmapped_target = rem_tgt
    return res


def detect_minimal_key(
    columns: list[str],
    rows: list[dict],
    *,
    max_columns: int = 3,
    uniqueness_floor: float = 0.98,
) -> tuple[list[str], float]:
    """Distinctness-ranked minimal unique key over a sample (UniqueKeyColumnDetector port).

    Rank single columns by distinctness, seed with the most distinct, then greedily add the
    column that most improves uniqueness until the floor is met or `max_columns` is hit.
    Returns (key_columns, uniqueness_ratio) — empty list if no sample."""
    n = len(rows)
    if n == 0 or not columns:
        return [], 0.0

    def ratio(cols: list[str]) -> float:
        return len({tuple(r.get(c) for c in cols) for r in rows}) / n

    ranked = sorted(columns, key=lambda c: ratio([c]), reverse=True)
    key = [ranked[0]]
    best = ratio(key)
    if best >= uniqueness_floor:
        return key, best

    for _ in range(max_columns - 1):
        candidate, cand_ratio = None, best
        for c in ranked:
            if c in key:
                continue
            r = ratio([*key, c])
            if r > cand_ratio:
                candidate, cand_ratio = c, r
        if candidate is None:
            break
        key.append(candidate)
        best = cand_ratio
        if best >= uniqueness_floor:
            break
    return key, best


def map_keys(
    source_keys: list[str],
    target_columns: list[str],
    column_mappings: dict[str, str] | None = None,
    *,
    src_samples: dict[str, set] | None = None,
    dst_samples: dict[str, set] | None = None,
) -> dict[str, str]:
    """Map each source key column to a target column: explicit mapping first, else the same
    name cascade, else positional fallback against remaining target columns."""
    out: dict[str, str] = {}
    used: set[str] = set()
    column_mappings = column_mappings or {}
    for i, sk in enumerate(source_keys):
        if sk in column_mappings and column_mappings[sk] in target_columns:
            out[sk] = column_mappings[sk]
        else:
            m = map_columns(
                [sk],
                [t for t in target_columns if t not in used],
                src_samples=src_samples,
                dst_samples=dst_samples,
            )
            if sk in m.mappings:
                out[sk] = m.mappings[sk]
            elif i < len(target_columns):  # positional fallback
                out[sk] = target_columns[i]
        if sk in out:
            used.add(out[sk])
    return out
