# Cross-Engine Compare — Benchmark Findings & Recommendations

**Scope:** Oracle ↔ Snowflake data compare at scale (10M rows × 30 columns), using the
`dv_platform` compare engine. Captures the benchmark harness, a connector performance bug found
and fixed, the engine's tiered-strategy behavior, `fail_fast` metrics, and a validated prototype
for the one architectural gap.

**Status:** correctness — strong; absolute performance — not yet production-grade for large tables,
with a specific fix prototyped below. Numbers are *indicative* (single runs, laptop → two clouds),
except where a signal clears the ~40% run-to-run noise floor by a wide margin.

---

## 1. Setup

- **Source of truth:** Oracle `DATAQ.DQ_BENCH_10M` (clean, 10M rows, 30 mixed-type cols, key `ID`).
- **Target:** Snowflake `DATAQ_BENCH.PUBLIC.DQ_BENCH_10M` (identical base) + diff variants:
  - `_D01` 1% scattered, `_D05` 5% scattered, `_D10` 10% scattered (every Nth row perturbed)
  - `_C10` 10% clustered (one contiguous `ID ≤ 1,000,000` block)
  - perturbation: `AMOUNT += 1`, `STATUS = 'CHANGED'`, `NOTE || ' [MODIFIED]'`.
- **Cross-engine hash parity:** Oracle `STANDARD_HASH(…, 'SHA256')` == Snowflake `SHA2(…, 256)`
  over a canonical pipe-joined, type-normalized row text. Verified identical at id = 1 … 10,000,000.
- Generators: `scripts/generate_bench_wide.py`, `scripts/copy_tpch_to_oracle.py`.

## 2. Correctness (the headline)

Every full-scan compare reported the **exact** expected diff count — 0 / 100K / 500K / 1M / 1M —
across a genuinely hard boundary (different number/date formatting, charset, NULL handling, hash
rendering). For a data-quality tool this matters more than any timing number. The canonicalization
holding across 10M rows × 30 mixed-typed columns is the strongest result here.

## 3. Connector performance bug (found + fixed)

The streaming path crawled at ~1K rows/s. Two compounding causes, both fixed:

1. **No index on the key.** The CTAS-built table had no index on `ID`, so every streaming window
   (`WHERE ID > :last ORDER BY ID FETCH FIRST N`) did a full scan + sort → ~O(n²) overall. Adding a
   unique index on `ID` halved window time.
2. **Tiny fetch batches.** `OracleConnector.execute()` used python-oracledb defaults
   (`arraysize=100`), i.e. ~1 network round-trip per 100 rows → a 50K window = ~500 round-trips to
   GCP (~23s). Set `arraysize`/`prefetchrows = 10000`.

**Result:** 50K-row window pull **36–50s → ~2s (~20×)**; Oracle fetch ~1K → ~50K rows/s. The fix
(`src/dvp/connectors/oracle/connector.py`) benefits every Oracle compare — streaming windows,
in-memory loads, and fingerprint leaf-pulls. *This bug silently taxed all Oracle compares in
production, not just this benchmark.*

## 4. Engine behavior (verified against design)

The tiered escalation works as designed:

| Case | Engine chosen | Evidence |
|---|---|---|
| Small table | `in-memory` | NATION (25 rows) full in-memory compare |
| Large, identical | `fingerprint` | base: 100 chunks checksummed in-DB, **no rows read** |
| Large, **clustered** diffs | `fingerprint` | C10: pruned 9,000,000 rows in-DB, pulled only 2,000,000 from 10 divergent leaves |
| Large, **scattered** diffs | `fingerprint` → **bails** → `streaming` | D0x: `61/61 chunks dirty (>60%) — bail`; pulls all raw rows |

The dispersion insight is correct: scattered diffs make `P(bucket clean) = 0.99^bucket_size ≈ 0`, so
bucket-level pruning is futile. The engine detects this and bails — **but it bails to pulling raw
rows, which is the weakness addressed in §6.**

## 5. `fail_fast` metrics (OFF vs ON, all post-fix)

| Variant | Pattern | Engine | OFF time | OFF diffs (exact) | ON time | ON diffs (lower-bound) | fail_fast Δ |
|---|---|---|--:|--:|--:|--:|--:|
| base | identical | fingerprint | 407s | 0 | 407s | 0 | 1.0× (none) |
| D01 | 1% scattered | streaming | 1,047s | 100,000 | 399s | 2,000 | 2.6× |
| D05 | 5% scattered | streaming | 1,627s | 500,000 | 342s | 10,000 | 4.8× |
| D10 | 10% scattered | streaming | 1,193s | 1,000,000 | 361s | 20,000 | 3.3× |
| C10 | 10% clustered | fingerprint | 453s | 1,000,000 | 744s | 100,000 | ~1× (none) |

**Reading:**
- `fail_fast` helps only on the **streaming** path (3–5×), by stopping after one window.
- It does **nothing** on the **fingerprint** path — the full in-DB checksum always runs; the C10
  ON > OFF inversion is checksum-phase variance, not a regression.
- The **sample gate already fast-rejects in ~30–70s** (before any engine runs), so `fail_fast`'s
  340–400s is a *slow* way to answer "do they differ," and its counts overshoot `max_diff_rows`
  (returns a whole window/leaf). As implemented it adds little over the sample gate.

## 6. The missing tier — row-hash merge-join (prototyped, validated)

**Problem:** on scattered diffs the engine bails from bucket-checksum to pulling *raw rows* (30
cols, both sides, ~20M rows of transfer). But both engines produce an identical canonical **row
hash** — so move `(key, row_hash)` pairs and join on them, fetching full rows only for mismatches.

**Prototype** (`scripts/rowhash_poc.py`): pushdown `(ID, row_hash)` from each engine, windowed
merge-join. Sanity verified (base → 0 diffs, D01 → 1,000 over a 100K range).

| Variant | Prod engine (OFF) | **Row-hash PoC** | Diffs | Exact? | Speedup |
|---|--:|--:|--:|:--:|--:|
| D01 (1% scattered) | 1,047s (streaming) | **281s** | 100,000 | ✅ exact | **3.7×** |
| D05 (5% scattered) | 1,627s (streaming) | **246s** | 500,000 | ✅ exact | **6.6×** |
| D10 (10% scattered) | 1,193s (streaming) | **248s** | 1,000,000 | ✅ exact | **4.8×** |
| C10 (10% clustered) | 453s (fingerprint) | **245s** | 1,000,000 | ✅ exact | **1.8×** |

Each PoC moved a flat ~1,440 MB (2 cols, both sides) and returned **exact** counts (not
`fail_fast` lower bounds).

**Key finding — dispersion stops being a cost driver.** Row-hash is **flat ~245–281s regardless of
diff rate or distribution** (1% vs 10%, scattered vs clustered), because it always scans everything
and moves only hashes. Contrast the production engine, which swings from 453s (clustered) to 1,627s
(5% scattered). Row-hash doesn't just beat raw streaming (3.7–6.6×) — it also **edged out the
fingerprint engine on the clustered case** (245s vs 453s), suggesting it may be a strong *default*
for high-dispersion large compares, not only a scattered-bail target. Mechanism: transfer 2 columns
not 30, hashing pushed down, compare by hash-equality not 30-field row compare.

**Nuance / not-yet-proven:** for *identical or low-diff* tables, fingerprint's prune-and-move-nothing
should still win (it confirms large clean regions with zero transfer, whereas row-hash always moves
all 10M hashes). The C10 245s-vs-453s edge is also cross-run (different warehouse warmth), so a
*controlled* fingerprint-vs-row-hash head-to-head on base + a low-diff case is needed before
declaring row-hash the universal default. Safe conclusion today: **row-hash is the right bail target
for scattered diffs; fingerprint stays for low-dispersion/identical.**

**Recommendation:** when `fingerprint` detects >60% buckets dirty, **bail to row-hash, not raw
streaming**. Keep raw streaming only as the correctness floor for column types where cross-engine
canonicalization can't be guaranteed. Fingerprint (prune when diffs are rare/clustered) and row-hash
(move hashes when diffs are scattered) are complementary, covering the full dispersion range.

## 7. Prioritized recommendations

1. **Add the row-hash merge-join tier** as the fingerprint bail target (§6). Highest ROI — removes
   the one real performance weakness. Prototype validated at 3.7× on the hardest case.
2. **Short-circuit `fail_fast` earlier** (at the sample gate / first dirty chunk), or reconsider it
   — the sample gate already fast-rejects; today `fail_fast` is coarse and redundant on fingerprint.
3. **Fix benchmark methodology** before quoting numbers to 2 sig figs: co-located runner (not a
   laptop → two clouds), dedicated warm warehouse, N ≥ 5 median, report variance.
4. **Keep the connector fetch fix** (now default) and **auto-ensure/validate a key index** exists
   (or warn) before streaming.
5. Parallelize streaming windows and drop the redundant per-window duplicate-key probe for unique
   keys.

## 8. Implemented — systematic-column skip guard

When the sample gate pins ~all divergence on **1–3 columns** (a shifted date/timezone/format) while
the rest match, the engine now **halts before the expensive compare** and returns a
`needs_decision` outcome instead of silently taking the slow scattered path. Rationale (from §4–6):
those 1–3 columns are exactly what makes every row's hash differ → scattered → streaming; skipping
them lets the rest prune fast.

- **Interaction:** halt-and-recommend. The job finishes fast in status `needs_decision` with a
  `decision_required` payload: `{columns, reason, rerun_skip:{ignore_columns:[…]}, rerun_full:{acknowledge_systematic:true}}`.
  The UI shows **[Skip these & compare the rest (fast)]** / **[Compare everything anyway (slow)]**,
  each re-running via the existing `PATCH options` + `:run`/`:submit` endpoints. New option
  `acknowledge_systematic` bypasses the guard.
- **Pre-existing bug fixed:** `ignore_columns` was only applied to the *schema* compare, never the
  *data* compare — so a "skipped" column still dirtied every hash bucket and forced the slow path.
  Now filtered out of `_run_data` (mapping, gate, engines) at the source.
- **Validated:** `scripts/test_systematic_guard.py` — a real-API, real-engine (no-mocks) functional
  test. Fixture: Snowflake `DQ_BENCH_10M_SYS` (UPDATED_DT shifted on every row; all else identical).
  Default → `needs_decision` (flags UPDATED_DT); skip → match, 0 diffs; acknowledge → 50,000 diffs.
  All 9 assertions pass.
- **UI remaining (spec only):** render the `needs_decision` status + two action buttons; no new API.

**Files:** `src/dvp/testing_engine/service.py` (guard + status + `ignore_columns` data-compare fix),
`scripts/test_systematic_guard.py` (functional test).

## 9. Methodology caveats

- Single runs, no warmup; run-to-run variance ≈ 40% (C10 ON > OFF; D05 500K diffs took longer than
  D10 1M diffs). Only the connector fix (3–20×) and the row-hash result (3.7×) clear the noise floor
  decisively.
- Runner is a laptop over home internet to two clouds — pessimistic for the transport-bound
  streaming path; a co-located deployment would look materially better. Pushdown/hash paths are far
  less sensitive to this (another reason to prefer them).
- Synthetic diffs (every-Nth-row vs one block) bracket reality; real diffs are bursty/correlated.
