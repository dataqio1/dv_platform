"""A tiny, dependency-free Bloom filter for the Find Column index.

A Bloom filter answers "is this value POSSIBLY in this column?" with no false negatives and
a tunable false-positive rate, in a few bytes per distinct value. We use it as the Phase-1
shortlist: a "no" lets us skip a table without touching it; a "maybe" is confirmed by an exact
live query (Phase 2), so false positives never reach the user.

Key properties we rely on:
  * `sized()` picks (m, k) from an expected element count + target FP rate.
  * two filters with the SAME (m, k) can be OR-merged — this is what makes incremental scans
    cheap: index the new rows into a delta filter and merge it into the stored one.
  * membership MUST use the same text representation the index was built from — the indexer
    feeds it the DB's `CAST(col AS TEXT)` form, and the query path tests the raw value, which
    is exactly what the Phase-2 `CAST(col AS TEXT) = 'value'` predicate compares.
"""

from __future__ import annotations

import hashlib
import math


class BloomFilter:
    __slots__ = ("m", "k", "bits")

    def __init__(self, m: int, k: int, bits: bytes | bytearray | None = None) -> None:
        self.m = max(8, int(m))
        self.k = max(1, int(k))
        nbytes = (self.m + 7) // 8
        if bits is None:
            self.bits = bytearray(nbytes)
        else:
            self.bits = bytearray(bits)
            if len(self.bits) != nbytes:  # tolerate a stored blob sized differently
                self.bits = (self.bits + bytearray(nbytes))[:nbytes]

    @classmethod
    def sized(cls, n_expected: int, fp: float = 0.01) -> BloomFilter:
        n = max(1, int(n_expected))
        fp = min(max(fp, 1e-6), 0.5)
        m = int(math.ceil(-(n * math.log(fp)) / (math.log(2) ** 2)))
        m = max(8, ((m + 7) // 8) * 8)  # byte-align
        k = max(1, int(round((m / n) * math.log(2))))
        return cls(m, k)

    def _positions(self, value: str):
        # Double hashing (Kirsch–Mitzenmacher): two independent hashes generate k indices.
        digest = hashlib.sha256(value.encode("utf-8")).digest()
        h1 = int.from_bytes(digest[:8], "big")
        h2 = int.from_bytes(digest[8:16], "big") | 1  # odd → full period
        m = self.m
        for i in range(self.k):
            yield (h1 + i * h2) % m

    def add(self, value: str) -> None:
        for pos in self._positions(value):
            self.bits[pos >> 3] |= 1 << (pos & 7)

    def __contains__(self, value: str) -> bool:
        return all(self.bits[pos >> 3] & (1 << (pos & 7)) for pos in self._positions(value))

    def merge(self, other: BloomFilter) -> None:
        """OR another filter in place. Caller guarantees matching (m, k)."""
        if other.m != self.m:
            raise ValueError("cannot merge Bloom filters with different sizes")
        for i in range(len(self.bits)):
            self.bits[i] |= other.bits[i]

    def bits_set(self) -> int:
        return sum(bin(b).count("1") for b in self.bits)

    def to_bytes(self) -> bytes:
        return bytes(self.bits)
