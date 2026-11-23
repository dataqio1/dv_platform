"""Column shape metadata + the pruning gates that run BEFORE the Bloom filter.

The Find Column index stores, per column, a handful of cheap scalar features (type, numeric
range, length range, character-class bitmask, the set of format masks, and — for low-cardinality
columns — the exact distinct values). A search derives the same features for the query value and
runs a cascade of gates; each gate is a *conservative superset* test, so it may keep a column
that doesn't actually contain the value (harmless — the live query confirms) but NEVER rejects a
column that does (the no-false-negative invariant the whole design rests on).

Gate order, cheapest → most selective:
  ① type / numeric      ② length      ③ numeric range      ④ charset subset      ⑤ format mask
Only columns that survive all of these reach the Bloom (high-cardinality) or an exact set
membership (low-cardinality). See PRD_Value_Locator.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# Character-class bits for the charset mask.
C_DIGIT = 1
C_UPPER = 2
C_LOWER = 4
C_SPACE = 8
C_PUNCT = 16
C_OTHER = 32

# A column with <= this many distinct values stores them exactly (no Bloom, no false positives).
LOW_CARD_MAX = 1024
# Format-mask capture caps — beyond these the mask gate self-disables (stays sound).
PATTERN_CAP = 64
PATTERN_MAXLEN = 40
# Numeric comparison is done with Decimal (exact), so no float precision concerns.


def char_class(ch: str) -> int:
    o = ord(ch)
    if 48 <= o <= 57:
        return C_DIGIT
    if 65 <= o <= 90:
        return C_UPPER
    if 97 <= o <= 122:
        return C_LOWER
    if ch in " \t\n\r\f\v":
        return C_SPACE
    if 33 <= o <= 126:
        return C_PUNCT
    return C_OTHER


def charset_mask(s: str) -> int:
    m = 0
    for ch in s:
        m |= char_class(ch)
    return m


def pattern_mask(s: str) -> str:
    """Digits→9, uppercase→A, lowercase→a, space→space, everything else kept literal.
    So '27519'→'99999', 'NC'→'AA', '2026-11-10'→'9999-99-99', 'a@b.co'→'a@a.aa'."""
    out = []
    for ch in s:
        c = char_class(ch)
        out.append("9" if c == C_DIGIT else "A" if c == C_UPPER else "a" if c == C_LOWER
                   else " " if c == C_SPACE else ch)
    return "".join(out)


def parse_number(s: str) -> Decimal | None:
    """Finite Decimal for a numeric string, else None (rejects NaN/Inf and non-numeric)."""
    s = s.strip()
    if not s:
        return None
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def is_date(s: str) -> bool:
    """Cheap ISO-ish date/datetime check (YYYY-MM-DD[ T]HH:MM[:SS])."""
    s = s.strip()
    if len(s) < 10 or s[4] != "-" or s[7] != "-":
        return False
    head = s[:10]
    if not (head[:4].isdigit() and head[5:7].isdigit() and head[8:10].isdigit()):
        return False
    return len(s) == 10 or s[10] in " T"


def is_date_type(data_type: str | None) -> bool:
    """Whether a column's declared source type is a date/time type. Find Column does NOT index
    these — date values don't have a single canonical text form, so they can't be matched
    reliably. Covers DATE / TIME / TIMESTAMP / DATETIME across sources."""
    t = (data_type or "").upper()
    return any(k in t for k in ("DATE", "TIME", "TIMESTAMP", "DATETIME"))


def is_binary_type(data_type: str | None) -> bool:
    """Binary / large-object / semi-structured types — not meaningful for exact value search and
    often huge or un-castable (TO_CHAR on a CLOB errors; a BLOB is bytes). Skip them entirely.
    Covers BLOB/CLOB/NCLOB, BINARY/VARBINARY/BYTEA/RAW/BYTES/IMAGE, Oracle LONG, and Snowflake
    semi-structured VARIANT/OBJECT/ARRAY/GEOGRAPHY/GEOMETRY."""
    t = (data_type or "").upper()
    return any(k in t for k in (
        "BLOB", "CLOB", "NCLOB", "BINARY", "VARBINARY", "BYTEA", "RAW", "IMAGE", "BYTES",
        "LONG", "GEOGRAPHY", "GEOMETRY", "VARIANT", "OBJECT", "ARRAY",
    ))


@dataclass
class IndexPolicy:
    """Thresholds for deciding whether a column is worth indexing for value search."""

    long_len: int = 100          # avg text length above which a column counts as "long"
    high_card_ratio: float = 0.5  # distinct/non-null above which it's "high cardinality"
    hard_max_len: int = 2000     # any value longer than this → treat as a document/blob, skip


def classify_column(
    *, data_type: str | None, non_null: int, distinct_est: float, avg_len: float,
    max_len: int, policy: IndexPolicy,
) -> tuple[bool, str]:
    """Decide whether to INDEX a column for value search, with a human-readable reason.

    Default is to index — we only IGNORE when there is a clear reason (zero value or
    disproportionate cost). The thresholds come from `policy`. All inputs are cheap aggregates
    (type, counts, lengths, approx distinct) — no values are scanned to make this call.

    Rules, in order:
      1. binary/LOB/semi-structured or date/time type → not searchable.
      2. no non-null values → nothing to find.
      3. oversized values (max length over the hard cap) → a document/blob, regardless of cardinality.
      4. large AND high-cardinality text → free prose / high-entropy tokens (long low-card enums and
         short high-card IDs are deliberately KEPT).
      5. otherwise → index.
    """
    if is_binary_type(data_type):
        return False, "binary/LOB type"
    if is_date_type(data_type):
        return False, "date/time type"
    if not non_null:
        return False, "no values"
    if max_len and max_len > policy.hard_max_len:
        return False, f"oversized values (up to {max_len} chars)"
    ratio = distinct_est / non_null if non_null else 0.0
    if avg_len > policy.long_len and ratio > policy.high_card_ratio:
        return False, (
            f"large high-cardinality text (avg {int(avg_len)} chars, ~{int(distinct_est)} distinct)"
        )
    return True, "indexed"


def value_features(value: str) -> dict:
    """Derive a query value's shape once, for gating against every column."""
    v = value.strip()
    return {
        "text": v,
        "len": len(v),
        "charset": charset_mask(v),
        "pattern": pattern_mask(v) if len(v) <= PATTERN_MAXLEN else None,
        "num": parse_number(v),
    }


def passes_meta_gates(vf: dict, meta: dict) -> bool:
    """True if the value could POSSIBLY be in the column, per the scalar/format metadata.
    Conservative: returns True on anything it can't rule out. Never a false 'no'."""
    # ② length
    mn, mx = meta.get("min_len"), meta.get("max_len")
    if mn is not None and mx is not None and not (mn <= vf["len"] <= mx):
        return False
    # ④ charset subset — every class in the value must have been seen in the column
    cm = meta.get("charset_mask") or 0
    if (vf["charset"] & cm) != vf["charset"]:
        return False
    # ① type + ③ numeric range
    if meta.get("is_numeric"):
        if vf["num"] is None:
            return False  # numeric-only column can't hold a non-numeric value
        num_min, num_max = meta.get("num_min"), meta.get("num_max")
        if num_min is not None and vf["num"] < Decimal(num_min):
            return False
        if num_max is not None and vf["num"] > Decimal(num_max):
            return False
    # ⑤ format mask — only when the column captured ALL its masks (else gate is unsound → skip)
    if meta.get("pattern_exhaustive") and meta.get("pattern_masks") is not None and vf["pattern"] is not None:
        if vf["pattern"] not in set(meta["pattern_masks"]):
            return False
    return True


class ColumnStats:
    """One-pass accumulator for a column's shape. Mergeable via `from_prior` seeding so an
    incremental scan continues from the stored profile."""

    def __init__(self) -> None:
        self.count = 0  # non-null values seen
        self.all_numeric = True
        self.all_integer = True
        self.all_date = True
        self.num_min: Decimal | None = None
        self.num_max: Decimal | None = None
        self.min_len: int | None = None
        self.max_len: int | None = None
        self.charset = 0
        self.patterns: set[str] = set()
        self.pattern_exhaustive = True
        self.distinct: set[str] = set()
        self.low_card = True

    @classmethod
    def from_prior(cls, p: dict | None) -> ColumnStats:
        s = cls()
        if not p:
            return s
        s.count = max(0, (p.get("row_count") or 0) - (p.get("null_count") or 0))
        s.all_numeric = bool(p.get("is_numeric"))
        s.all_integer = p.get("inferred_type") == "integer"
        s.all_date = p.get("inferred_type") == "date"
        if p.get("num_min") is not None:
            s.num_min = Decimal(p["num_min"])
        if p.get("num_max") is not None:
            s.num_max = Decimal(p["num_max"])
        s.min_len = p.get("min_len")
        s.max_len = p.get("max_len")
        s.charset = p.get("charset_mask") or 0
        if p.get("pattern_exhaustive") and p.get("pattern_masks") is not None:
            s.patterns = set(p["pattern_masks"])
        else:
            s.pattern_exhaustive = False
        if p.get("is_low_card") and p.get("distinct_values") is not None:
            s.distinct = set(p["distinct_values"])
        else:
            s.low_card = False
        return s

    def add(self, text: str) -> None:
        self.count += 1
        n = len(text)
        self.min_len = n if self.min_len is None else min(self.min_len, n)
        self.max_len = n if self.max_len is None else max(self.max_len, n)
        self.charset |= charset_mask(text)
        if self.all_numeric:
            d = parse_number(text)
            if d is None:
                self.all_numeric = self.all_integer = False
            else:
                if d != d.to_integral_value():
                    self.all_integer = False
                self.num_min = d if self.num_min is None else min(self.num_min, d)
                self.num_max = d if self.num_max is None else max(self.num_max, d)
        if self.all_date and not is_date(text):
            self.all_date = False
        if self.pattern_exhaustive:
            if n > PATTERN_MAXLEN:
                self.pattern_exhaustive = False
                self.patterns = set()
            else:
                self.patterns.add(pattern_mask(text))
                if len(self.patterns) > PATTERN_CAP:
                    self.pattern_exhaustive = False
                    self.patterns = set()
        if self.low_card:
            self.distinct.add(text)
            if len(self.distinct) > LOW_CARD_MAX:
                self.low_card = False
                self.distinct = set()

    def finalize(self) -> dict:
        is_numeric = self.all_numeric and self.count > 0
        if is_numeric:
            inferred = "integer" if self.all_integer else "decimal"
        elif self.all_date and self.count > 0:
            inferred = "date"
        else:
            inferred = "string"
        return {
            "inferred_type": inferred,
            "is_numeric": is_numeric,
            "is_date": inferred == "date",
            "num_min": str(self.num_min) if (is_numeric and self.num_min is not None) else None,
            "num_max": str(self.num_max) if (is_numeric and self.num_max is not None) else None,
            "min_len": self.min_len,
            "max_len": self.max_len,
            "charset_mask": self.charset,
            "pattern_masks": sorted(self.patterns) if self.pattern_exhaustive else None,
            "pattern_exhaustive": self.pattern_exhaustive,
            "is_low_card": self.low_card,
            "distinct_values": sorted(self.distinct) if self.low_card else None,
            "distinct_est": len(self.distinct) if self.low_card else None,
        }
