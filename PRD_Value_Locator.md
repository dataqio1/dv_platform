# PRD & Architecture Spec — Value Locator

> **Product:** Value Locator (working name — "DataFind" / "Reverse Lookup" also candidates)
> **Status:** Draft for review
> **Author:** Product (dhiraj.peechara@dataq.io)
> **Date:** 2026-11-10
> **Platform:** Vexdata (DataQ) — new product alongside Data Quality & Data Compare

---

## 1. Summary

Value Locator lets a user paste a **value** (or a small set of values) and instantly see **which tables and columns contain it**, then drill into **live sample rows**. It answers the everyday question *"I have a value and I don't know where it lives."*

It works by scanning each source once, computing a compact **approximation profile per column** (primarily a **Bloom filter** for exact-match membership, plus cheap pruning/ranking sketches), and storing those profiles in an index. At query time a **two-phase search** shortlists candidate columns from the index (approximate, instant, zero source rows touched), then runs an **exact live query** against the real source to eliminate false positives and fetch sample data.

---

## 2. Goals & Non-Goals

### Goals (MVP)
- **G1.** Given a single exact value, return the tables/columns that contain it, ranked, across all authorized connections.
- **G2.** Given **two or more exact values**, return tables where they **co-occur in the same row** (e.g. zip `27519` AND state `NC`) — the headline impact-analysis workflow.
- **G3.** Show **real, confirmed sample rows** for each hit (no false positives ever surfaced to the user).
- **G4.** Respect the existing **ACL/permission model** and mask classified/PII columns in previews.
- **G5.** Index refresh via **incremental scan**, configurable per dataset, **at most once per day** by default.

### Non-Goals (MVP — deferred)
- Substring / "contains" / fuzzy matching (V2 — n-gram index).
- Approximate frequency surfacing / Count-Min (V2).
- Cross-column similarity / join discovery (V3 — MinHash).
- Lineage / "value journey" visualization (V3).
- Near-real-time (sub-daily) freshness.

---

## 3. Primary Driver & Use Cases

**Anchor persona: data engineer doing impact analysis** — *"where does this ID / value flow?"* — closest to existing DQ/Compare users, with the explicit requirement for **multi-value co-occurrence** within a table.

| # | Use case | Input | Expected result |
|---|---|---|---|
| UC1 | Locate a single value | `27519` | All authorized tables/columns containing `27519`, ranked, with sample rows |
| UC2 | Co-occurrence in same table/row | `27519` (zip) + `NC` (state) | Tables where a **single row** has both values; shows which column held each |
| UC3 | Value + column hint | `27519` as `zip*` | Same as UC1 but restricted to columns matching the hint (reduces noise & combinatorics) |
| UC4 | Scoped search | `A-7781` in connection `orders_db` | Hits within the chosen connection/schema only |

Secondary personas (value, not MVP-blocking): data stewards/privacy (DSAR, erasure — naturally served by multi-value search + PII masking, prioritize in V2), support/ops (debugging), analysts (find the join table).

---

## 4. Key Design Decision — Two-Phase Search

Approximation structures answer *"does value V **possibly** exist in this column?"* — **no false negatives, some false positives.** They are never shown directly to the user.

```
Phase 1 — SHORTLIST (approximate, in-memory index, milliseconds, 0 source rows)
    test V against every authorized column's Bloom filter (after type/zone pruning)
    → candidate columns, grouped by table

Phase 2 — CONFIRM & SAMPLE (exact, live query against the real source, on demand)
    run SELECT … WHERE <predicates> LIMIT N against candidate tables
    → eliminates false positives AND returns the sample rows the user wants
```

**Why this matters for co-occurrence (UC2):** a Bloom index is per-column, so Phase 1 can only say "table T has some column matching `27519` and some column matching `NC`." It **cannot** prove both values are in the same row. Phase 2 resolves this precisely:

```sql
SELECT * FROM T WHERE zip = '27519' AND state = 'NC' LIMIT 50;
```

- Default co-occurrence semantic = **same row** (conjunctive `AND`). A looser **same-table** mode is offered as an option.
- If values match multiple candidate columns, Phase 2 tries ranked column assignments (bounded, see §7.3). Column hints (UC3) collapse the combinatorics.

Phase 2 **reuses the existing connection + sample-data layer** — minimal new infrastructure.

---

## 5. Approximation Profile (per column)

MVP bundle — exact membership + cheap pruning + ranking:

| Structure | Answers | Role |
|---|---|---|
| **Bloom filter** | exact-match membership | Backbone of Phase 1; no false negatives |
| **Min/Max + null/row counts (zone map)** | range pruning, basic profile | Reject columns V can't be in before testing Bloom; near-free |
| **HyperLogLog** | distinct-count estimate | Rank candidates (high uniqueness ⇒ likelier key/ID); sizes the Bloom |
| **Type / format signature** (data type, length histogram, pattern class) | type-based pruning | V looks like a 5-digit code ⇒ skip incompatible columns first |

Deferred: **n-gram Bloom** (substring, V2), **Count-Min Sketch** (frequency, V2), **MinHash** (similarity, V3). All of these — and the MVP structures — are **mergeable**, which is what makes incremental scan cheap (§8).

### 5.1 Normalization — the #1 correctness risk
`"123"` vs `123` vs `0123` vs `" 123 "` vs `123.0` must match consistently. For each cell we index a **canonical set** of normalized forms and test the query value against the **same** set:
- raw
- trimmed
- case-folded (for text)
- type-coerced canonical (numeric/date canonical form; strip leading zeros only when the column is numeric, preserve them for codes/zips)

Normalization flags per column are stored in `vl_column_profile.normalization_flags` so Phase 2 predicates are built identically to how the Bloom was populated. **Zips and codes must NOT be numeric-normalized** — `27519` is a string code, not the integer 27519.

### 5.2 Bloom sizing
Target false-positive rate ~1% (configurable). Size `m`,`k` per column from the HLL distinct estimate so high-cardinality columns get larger filters. Store `bloom_m`, `bloom_k`, `bloom_fp_rate`, and the hash seed with the profile.

### 5.3 Deletes
Standard Bloom can't remove values; deleted source rows leave the filter over-inclusive. **Accepted** — Phase 2's exact verify hides the staleness from users. A **periodic full rebuild** (configurable, less frequent than incremental) resets drift. No counting-Bloom complexity in MVP.

---

## 6. Architecture

```
                 ┌───────────────────────────────────────────────┐
  SCAN (batch /  │  Spark profiling job (dms_sample / executor)   │
  incremental)   │  per column → Bloom + zonemap + HLL + type sig │
                 │  extends detectcolumnname profiling            │
                 └───────────────┬───────────────────────────────┘
                                 │ sketch blobs + metadata
                 ┌───────────────▼───────────────┐
  INDEX STORE    │  blobs → S3  ;  metadata → MariaDB (Flyway)    │
                 └───────────────┬───────────────┘
                                 │
                 ┌───────────────▼───────────────┐
  QUERY SERVICE  │  Phase 1: test values → candidates (resident) │
  dms-value-     │  Phase 2: live conjunctive SELECT … LIMIT N    │
  locator (new)  │  + ACL filter + PII masking + audit            │
                 └───────────────┬───────────────┘
                                 │
                 ┌───────────────▼───────────────┐
  UI             │ search box (1..n values) → ranked hits →       │
                 │ expand → live masked sample rows               │
                 └────────────────────────────────┘
```

### Mapping to existing platform assets (maximize reuse)
- **Scan engine:** `dms_sample` (Spark 3.5.5) + `dms-executor` orchestration. **Extend `detectcolumnname`** (already does column profiling, type inference, key detection) to also emit the sketch bundle — do not greenfield.
- **Connections & sample data:** reuse the existing connection manager + sample-data retrieval (the `demo-server` connection layer) for Phase 2. Multi-dialect SQL generation goes through the existing abstraction.
- **Index metadata:** MariaDB + Flyway (new `vl_*` tables, §9). Sketch blobs live in **S3** referenced by key — not in BLOB columns.
- **New service:** `dms-value-locator` (Java 21, Spring Boot 3.4.x, package `io.dataq.valuelocator`), JWT-stateless like the rest of the fleet, routed via `dms-gateway`.
- **UI:** new product surface; search bar → ranked result list (connection · schema · table · column matches · est. rows · confidence) → expand for live samples.

### Scale consideration (spike before committing)
Testing a Bloom is a handful of hash ops — 100k+ columns is trivial CPU-wise. The real constraint is **keeping filters resident** (potentially GBs). Mitigations to validate in a spike: type/zone pre-filter before touching Blooms, a per-node memory budget, and a tiered/lazy-load store for cold datasets.

---

## 7. Search Semantics & API

### 7.1 `POST /value-locator/search`
```jsonc
{
  "values": [
    { "value": "27519", "columnHint": "zip*", "typeHint": "code" },
    { "value": "NC",    "columnHint": "state*" }
  ],
  "matchMode": "co-occurrence",   // "co-occurrence" (same row) | "same-table" | "any-table"
  "scope": { "connectionIds": [], "schemas": [] },  // empty = all authorized
  "limitPerHit": 50,
  "verify": true                   // true = run Phase 2 on top candidates inline
}
```

Response:
```jsonc
{
  "hits": [
    {
      "connection": "orders_db",
      "schema": "public",
      "table": "customer_address",
      "columnMatches": [
        { "value": "27519", "column": "zip",   "confidence": "high" },
        { "value": "NC",    "column": "state", "confidence": "high" }
      ],
      "estimatedRows": 1200,
      "verified": true,            // Phase 2 confirmed same-row co-occurrence
      "datasetId": 8891
    }
  ],
  "warnings": [ "3 datasets skipped: last scan > 7 days old (stale)" ]
}
```

- Use the platform `ApiResponse<T>` wrapper; surface non-fatal issues (stale index, unverifiable candidates, permission-filtered tables) via `warnings` per global convention.

### 7.2 `POST /value-locator/samples`
Fetch live masked sample rows for a confirmed hit on demand (lazy — not fetched for every candidate):
```jsonc
{ "datasetId": 8891, "predicates": [ {"column":"zip","value":"27519"}, {"column":"state","value":"NC"} ], "limit": 50 }
```

### 7.3 Phase-1 logic by mode
- **any-table / single value:** candidate columns = all authorized columns whose Bloom says "maybe V" (after type/zone pruning). Group by table, rank by HLL/zone fit.
- **co-occurrence (same row):** candidate tables = tables where **every** value matches ≥1 column. For each table, form ranked **column-assignment combinations** (v1→colA, v2→colB …), bounded to **top-K per value by type compatibility & cardinality** (default K=5; column hints collapse this). Phase 2 runs the conjunctive query for each assignment until a hit is found.
- **same-table (looser):** like co-occurrence but Phase 2 confirms each value exists in the table independently (no `AND` on a single row).

### 7.4 Scan management
- `POST /value-locator/scan-config` — create/update a dataset's scan config (§8).
- `POST /value-locator/scan-config/{id}/run` — trigger a scan now.
- `GET /value-locator/scan-run/{id}` — status/progress.

---

## 8. Scan & Freshness

- **Incremental by default**, driven by a per-dataset **watermark column** (e.g. `updated_at` or an auto-increment id). Incremental scan reads rows since the last watermark, builds a **delta profile with identical Bloom params**, and **merges** (bitwise-OR Bloom, merge HLL, update min/max) into the stored profile. All MVP sketches are mergeable → incremental is cheap.
- **Frequency:** configurable per dataset, **capped at once per day** (low priority per product call). Config stored in `vl_scan_config` (cron or interval). Default daily.
- **Periodic full rebuild** (configurable, e.g. weekly/monthly) to clear delete-drift (§5.3).
- **Staleness surfaced** in results (`last_scanned_at`) and as a warning when a dataset is older than a threshold.

---

## 9. Data Model (MariaDB, Flyway — idempotent `IF NOT EXISTS`)

> New migration files only; never edit existing migrations. Naming `V{n}__value_locator_*.sql`.

**`vl_scan_config`** — per dataset scan configuration
`id, connection_id, catalog, schema_name, table_pattern, include_columns, exclude_columns, watermark_column, scan_cron, full_rebuild_cron, bloom_target_fp, enabled, created_at, updated_at, last_run_at`

**`vl_scan_run`** — each scan execution
`id, scan_config_id, type (INCREMENTAL|FULL), status, started_at, finished_at, columns_scanned, rows_scanned, error_message`

**`vl_dataset`** — a scanned table
`id, connection_id, catalog, schema_name, table_name, row_count_est, last_scanned_at, last_scan_run_id, UNIQUE(connection_id, catalog, schema_name, table_name)`

**`vl_column_profile`** — per column (the core index row)
`id, dataset_id, column_name, ordinal, source_data_type, inferred_type, distinct_est, null_count, row_count, min_val, max_val, bloom_s3_key, bloom_m, bloom_k, bloom_fp_rate, bloom_seed, normalization_flags, pii_class, last_scanned_at`

**`vl_search_audit`** — governance
`id, user_id, values_hash, scope, match_mode, hit_count, created_at`
(store a hash/redacted form of searched values — searching is sensitive; don't log raw PII in the clear.)

Sketch blobs (serialized Bloom, optionally HLL for merges) in **S3**, keyed by `bloom_s3_key`.

---

## 10. Governance & Security (not optional for this product)

A value-search tool that ignores permissions is a data-exfiltration feature. Non-negotiables:
- **ACL enforcement** in both phases — users only see datasets/columns they're authorized for (reuse existing multi-layer permission model; filter Phase-1 candidates and Phase-2 queries).
- **Masking** of classified/PII columns in sample previews; tie to a classification pass during scan (`vl_column_profile.pii_class`).
- **Audit** every search (`vl_search_audit`) — who searched for what, when, how many hits.
- **Don't fail silently** (global rule): stale index, permission-filtered results, and unverifiable candidates all surface as warnings via `ApiResponse.successWithWarnings`.
- Bonus: PII auto-classification doubles as a **privacy feature** (feeds the V2 DSAR/erasure workflow).

---

## 11. Phasing

| Phase | Scope |
|---|---|
| **MVP** | Bloom + zonemap + HLL + type sig · exact match · single value + same-row co-occurrence · column hints · two-phase verify · live masked samples · ACL + audit · incremental scan (daily cap, configurable) + periodic full rebuild |
| **V2** | Substring/contains (n-gram) · approximate frequency (Count-Min) · PII auto-classification + DSAR "find all locations of these values" batch mode · richer scheduling |
| **V3** | Cross-column similarity & join/relationship discovery (MinHash) · value-journey / lineage overlay · catalog integration |

---

## 12. Risks & Open Questions

| Risk / question | Mitigation / needs decision |
|---|---|
| **Normalization mismatches** silently break membership (§5.1) | Canonical-form set indexed & queried identically; codes vs numbers handled explicitly. **Highest-priority correctness test.** |
| **Resident-memory footprint** at 100k+ columns (§6) | Spike: type/zone pre-filter, memory budget, tiered store before committing |
| **Co-occurrence combinatorial blowup** when values match many columns | Top-K assignment cap + column hints + type-compatibility ranking (§7.3) |
| **Delete drift** in Bloom filters | Accepted; Phase 2 verifies; periodic full rebuild |
| **Phase-2 load on prod sources** | `LIMIT N`, candidate cap, respect source rate limits; consider read-replica/sample connections |
| **Data types across dialects** (how do different DBs represent the same value) | Normalize to a common canonical form at scan AND query time |

### Decisions still needed from product/eng
1. **New standalone service vs module in `dms`?** (Recommend standalone `dms-value-locator` for isolation & scaling; confirm.)
2. **Co-occurrence default = same row** — confirm this is the desired default over same-table.
3. **Sample-query target:** hit prod source directly, or require a read-replica / sample connection for Phase 2?
4. **PII classification source:** build during scan now, or integrate an existing classifier?

---

## 13. Success Metrics
- Time-to-locate a value (baseline: minutes of manual querying → target: seconds).
- % of searches that return a verified hit.
- Adoption: searches/user/week among data engineers.
- Index freshness SLA adherence (datasets scanned within configured window).
- Zero unauthorized-data-exposure incidents (ACL/masking correctness).
