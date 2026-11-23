# 05 — Data Testing (Compare Engine)

Covers requirement group 2 (a–d): count compare, schema compare, cell-level data
compare across Snowflake ↔ Oracle (and any combination), with automatic
compare-key and compare-column detection.

> **Status:** reflects the implemented engine (`src/dvp/testing_engine/`). The
> original clean-room design was informed by the Scala `datadiff` engine; the
> *algorithms* that proved out (pushdown fast-paths, sample+source-validate key
> detection, hash-based pruning, type normalization) were kept and the legacy
> request/response contracts dropped. Since then the strategy selector has been
> replaced by a **tiered, self-routing engine set** (recursive-hash,
> Snowflake-hash, fingerprint, streaming, in-memory, Spark) fronted by a cheap
> **sample gate** — see §7. Benchmark evidence for the tiering lives in
> [`cross-engine-compare-findings.md`](../cross-engine-compare-findings.md).

## 0. TL;DR — how a data compare actually runs

1. Introspect both sides' columns; normalize to a canonical type system (§6).
2. Detect (or accept) the **compare key** (§4) and align **compare columns**,
   auto-mapping renamed columns if needed (§5).
3. Run a **stage-0 sample gate** (~120 rows) to catch systematic/encoding
   differences and pick a routing hint *before* touching the full table (§7.1).
4. Route to the **cheapest correct engine** for the data characteristics — same
   DB → recursive-hash; both Snowflake → hash pushdown; large cross-DB →
   fingerprint (bail to streaming on scattered diffs); small → in-memory (§7.2).
5. Every engine produces the **same five result buckets** (§8) and is bounded by
   the same **budget guards** (time / query-count / memory) and **fail-fast**
   semantics (§9).

## 1. Compare job model

```
compare_job
├── id, group_id, org_id, name
├── kind          = count | schema | data
├── source        { connection_id, database, schema, table, filter?, sql? }
├── target        { connection_id, database, schema, table, filter?, sql? }
├── options       { key?, columns?, column_mapping?, tolerance?, ignore_spaces?,
│                   null_equals_empty?, case_insensitive?, bad_record_limit,
│                   engine?, fail_fast?, max_diff_rows?, max_queries?,
│                   timeout_s?, memory_gb?, ignore_columns?,
│                   acknowledge_systematic? }
├── status        = queued | running | succeeded | failed | partial | needs_decision
├── result_ref    → result document (metadata-DB summary + ResultStore detail)
└── created_by / timestamps
```

A job can compare a set of table pairs (batch), continuing past a per-table
failure and reporting `partial` (proven pattern from datadiff — one bad table
doesn't fail the whole run). A compare can also finish in **`needs_decision`**
when the sample gate finds a handful of systematically-different columns and asks
the user how to proceed (§7.4).

User-supplied `filter` predicates and raw `sql` sub-selects are validated by
`sql_guard` (§10) **after** `{{variable}}` substitution, so neither user input nor
injected variables can smuggle in a second statement, comment, set-operator, or
DDL/DML keyword.

## 2. Count compare (2a)

- Pure pushdown: `SELECT COUNT(*) [WHERE filter]` on each side; only scalars cross
  the wire. No Spark.
- Comparison operators (tolerance-aware): `=`, `>`, `<`, `>=`, `<=`, `abs<=`,
  `abs>=`, plus a percentage tolerance (`COUNT_TOLERANCE_PCT`).
- Result: `{ src_count, dst_count, diff, diff_pct, match: bool }`.
- Per-table errors are captured as an `EXCEPTION` status on that pair (not a hard
  fail), so a batch of tables completes with partial results.

## 3. Schema compare (2b)

- Pull each side's column metadata (`DESCRIBE` / `INFORMATION_SCHEMA`); merge
  per-table so mismatched source/target representations stay aligned.
- Normalize types to a **canonical type system** (see §6) before comparing, so
  Oracle `NUMBER(38,0)` and Snowflake `NUMBER(38,0)` are recognized as equal, and
  `VARCHAR2` ↔ `VARCHAR` matches.
- Report per column: present-on-both / source-only / target-only; type match
  (raw + canonical); nullability; ordinal position; PK/index where the source DB
  exposes it (skipped for Snowflake, which lacks enforced constraints in
  metadata — same caveat datadiff handles).
- Result: list of `SchemaCompareRecord { column, src_type, dst_type,
  canonical_match, nullability_match, status }`.

## 4. Compare-key detection (2d)

`keydetect.detect_key()` — a two-tier method: cheaply rank candidates, then
greedily grow a composite and confirm it on the full table.

1. **Keyable-column filter** (`is_keyable`): exclude `FLOAT`/`DOUBLE`/`REAL` and
   Oracle `BINARY_FLOAT`/`BINARY_DOUBLE` and `TIMESTAMP`/`BINARY` (precision /
   granularity make them unreliable join keys); keep `STRING`, integral types,
   `DATE`, and `DECIMAL` (a `NUMBER(38,0)` surrogate id is a great key).
2. **Name-priority scoring** (`name_priority`, lower = better): `id` → 0; `*_id` /
   `*_key` → 1; contains `id`/`key` → 2; `code`/`number`/`num` → 3; general → 5;
   `date`/`time` → 8 (deprioritized). Ties broken by non-nullable first, then
   alphabetical.
3. **Sample-based greedy composite**: for each candidate (ranked by observed
   `sample_distinct_ratio` descending, then name priority), try it as a single
   key; if its sampled distinct-ratio ≥ **`uniqueness_threshold = 0.99`**,
   confirm it. Otherwise grow a composite one column at a time up to
   **`max_cols = 3`**, re-checking the sampled ratio.
4. **Source validation via pushdown** (`validate_unique_full`): confirm the
   proposed key on the *full* table (`COUNT(*) == COUNT(DISTINCT key)`, a scalar —
   no data pull) before accepting it; fall back to the next candidate if it fails.

The detected key is returned to the user, who can override it. The same key must
be valid on both sides for a data compare; if not, we report it clearly rather
than producing garbage.

## 5. Compare-column detection & auto-mapping (2d)

- **Default**: compare all columns present on both sides (the intersection),
  excluding key columns.
- **`ignore_columns`**: drop audit/technical columns (`_LOADED_AT`, etc.). These
  are removed at the *source* of `_run_data` — from the mapping, the sample gate,
  and every engine — so an ignored column never dirties a hash bucket and forces a
  slow path (a bug that previously only filtered the schema compare; now fixed).
- **Automatic column mapping** (`column_mapping.map_columns`) aligns
  differently-named columns through a first-match-wins cascade, each layer with a
  confidence score:

  | Layer | Rule | Confidence |
  |---|---|---|
  | provided | user-supplied `src → tgt` pairs | 1.00 |
  | exact | case-insensitive name equality | 1.00 |
  | prefix-strip | equal after removing `c_`/`src_`/`dim_`/`stg_`/… prefixes | 0.90 |
  | abbreviation | token-wise via `ABBREVIATION_MAP` (`customer`↔`cust`, `quantity`↔`qty`, …) | 0.80 |
  | suffix / containment | significant suffix overlap (`order_id`↔`order_reference`) | 0.75 |
  | data similarity | Jaccard overlap of sampled distinct values | ≥ 0.30 |
  | profile similarity | sample-invariant fingerprint (type + pattern: email/uuid/date/… + length/magnitude) | ≥ 0.55 |

  Only mapped columns are compared; unmapped columns on either side are reported.

## 6. Type normalization (the correctness core)

`typemap` is the authoritative judge — **every** engine (in-memory, recursive-hash,
fingerprint, streaming, Snowflake-hash, Spark) must produce byte-identical verdicts
on identical data. Both sides are mapped to a canonical type and values normalized
so cross-DB representational differences don't cause false mismatches:

| Canonical | Maps from | Comparison rule |
|-----------|-----------|-----------------|
| `NUMERIC` | Oracle `NUMBER`, Snowflake `NUMBER/DECIMAL/FLOAT`, int types | Parsed to **`Decimal`** (exact — no float round-trip loss), so `1.50 == 1.5`; optional absolute tolerance. |
| `STRING` | `VARCHAR`, `VARCHAR2`, `CHAR`, `TEXT`, `CLOB` | Optional `trim` (ignore_spaces), optional case-insensitive. |
| `DATE` | `DATE` | Compare calendar date only (time-of-day ignored). |
| `TIMESTAMP` | `TIMESTAMP*`, `TIMESTAMP_TZ/LTZ/NTZ`, `DATETIME` | Parse to instant, normalize to UTC, compare at microsecond precision; optional tolerance. |
| `BOOLEAN` | `BOOLEAN`, `BIT`, `NUMBER(1)`/`CHAR(1)` flags | Coerce truthy/falsy (`1/t/true/yes` ↔ `0/f/false/no`). |

When two sides carry **incompatible** canonical types (e.g. `NUMERIC` vs
`TIMESTAMP`), `resolve_compare_type` falls back to `STRING` comparison rather than
guessing; otherwise it compares at the more-structured type
(`NUMERIC > TIMESTAMP > DATE > BOOLEAN > STRING`).

Null handling (configurable, datadiff-compatible defaults): `NULL` and empty
string `""` are treated as equivalent when `null_equals_empty=true` (default).

**Pushdown parity — `sql_canon`.** For engines that hash *inside the database*
(recursive-hash, fingerprint, Snowflake-hash), `sql_canon` is the single source of
truth for how each dialect renders a value to canonical **text** before hashing —
`numeric_text` (fixed 10-dp, trailing-zero-stripped), `temporal_text`
(`YYYY-MM-DD[ HH24:MI:SS.FF6]`), `boolean_text`, `string_text`. This guarantees an
Oracle row and its Snowflake twin fold to the *same* bytes, so their hashes match
iff `typemap.values_equal` would. Verified identical across 10M rows × 30 mixed
columns (see findings §2).

## 7. Data compare — tiered engine architecture (2c)

There is no standalone "strategy selector" object — routing lives in
`service._run_data`, which picks the cheapest correct engine from the data
characteristics (same account? same dialect? single numeric key? table size?
memory budget?). (A legacy `strategy.py` enum exists but is **not wired in**; the
live decision tree below is authoritative.)

### 7.1 Stage-0 sample gate (`sample_gate.precheck`)

Before any full-table engine runs on a cross-engine compare, pull a **stratified
~120-row sample** (head + tail by key, deduped) from the source, fetch the matching
keys on the target, and compare them in memory. This cheap probe decides the route:

- **All compared columns ~100% different** (per-column match ≤ `0.05`) → `stream`
  (hashing can't prune when every row differs).
- **Heavy divergence** (> `0.80` of common rows differ) → `stream`.
- **Encoding-suspect columns** — values match ≥ `0.95` but canonical text agrees
  < `0.95` → `stream` (in-DB hashing would be unreliable for those columns).
- **Too few shared keys** (common < `20`) → `inconclusive` (don't trust the sample).
- Otherwise → `proceed` (hash-based pruning is safe).

The gate typically settles "do these even differ, and how?" in ~30–70s, well
before an engine starts.

### 7.2 Engine selection (decision tree)

```
_run_data(source, target, options):
    if same_db and single_numeric_key and no_column_mapping
       and recursive_hash.supported():
          → recursive_hash            # §7.3  in-DB recursive range-checksum
    elif both_snowflake and tolerance == 0 and snowflake_hash.supported():
          → snowflake_hash            # §7.5  per-key HASH() pushdown (+ optional precheck)
    elif big cross-DB table (and not forced to stream):
          sample_gate.precheck(...)
          if fingerprint.supported():
                try  → fingerprint    # §7.6  cross-engine MD5 checksum bisection
                except BailToStreaming: → streaming
          else:  → streaming          # §7.7  bounded-window correctness floor
    elif requested == "spark" and spark available:
          → spark                     # §7.8  opt-in Spark path
    else:
          → inmemory                  # §7.4  load both sides, compare_rows()
```

`engine` option values: `auto` (default), `recursive`, `hash`, `hash_force`,
`fingerprint`, `streaming`, `spark`. A specific request that isn't applicable to
the data (e.g. `recursive` on a non-numeric key) returns a clear error rather than
silently falling back. "Big" is decided from estimated in-memory size vs
`memory_gb`, with a `_STREAM_ROW_THRESHOLD = 250,000`-row backstop.

### 7.3 Recursive range-checksum — same DB (`recursive_hash`)

Fastest path when both sides live on the **same server, same dialect**
(`snowflake`/`oracle`/`sqlite`), single numeric key, `tolerance == 0`. Nothing but
scalars move until a genuine difference is found.

1. **Whole-table checksum**: `(COUNT, SUM(row_hash))` on each side; equal → tables
   identical, return 100% match with **zero rows moved**.
2. **Global key range**: `MIN/MAX(key)` merged across both sides (`hi` made
   exclusive).
3. **Recursive descent**: split each range into `BRANCHING = 8` buckets; checksum
   each bucket on both sides; **prune** buckets whose `(count, hash)` match;
   recurse into those that differ. Shrink to a leaf when a range's count ≤
   `leaf_limit` or width collapses.
4. **Leaf detail**: pull only the leaf ranges' rows on both sides and run
   `compare_rows` for exact cell-level attribution.

Guarded by a 100k-iteration recursion cap plus the shared time/query/cancel budgets
(§9).

### 7.4 In-memory cell compare (`rowcompare.compare_rows`)

The canonical, pure comparator — the reference every other engine must agree with,
and the path for small or unmapped compares. Group both sides by key, then classify
each key into one of five buckets:

- **match** — exactly 1:1 rows and all compared cells equal.
- **value_difference** — 1:1 rows, ≥ 1 compared cell differs.
- **source_only** / **target_only** — key present on one side only.
- **ambiguous** — many-to-many (> 1 row per key on a side); flagged, not
  cell-diffed.

Cell equality is `typemap.values_equal`. Non-compared columns ride along as
`compared=false` cells and never affect the verdict. Samples are capped
(`bad_record_limit` per bucket, default 100; `pass_sample_limit` matched, default
50).

### 7.5 Snowflake hash pushdown (`snowflake_hash`)

Both sides in Snowflake, `tolerance == 0`. Push a per-key aggregation
(`COUNT`, `MIN/MAX(HASH(normalized_payload))`) into Snowflake so matching rows
never move; classify keys into the five buckets from the aggregates (1:1 +
equal-hash → match; count/hash mismatches → the other buckets); fetch full rows
only for a bounded sample of differing keys via a row-constructor `IN (…)`. An
optional sample precheck can short-circuit (`hash_sample`) when the sample already
settles the verdict.

### 7.6 Cross-engine fingerprint (`fingerprint_compare`)

The workhorse for large **Oracle ↔ Snowflake** compares (`FINGERPRINT_DIALECTS =
{sqlite, snowflake, oracle}`, single numeric key, `tolerance == 0`). Bandwidth-
optimal: move checksums, not rows.

1. **Counts + key range**; empty both sides → done.
2. **Size-adaptive chunking**: `nchunks = min(512, ceil(max(n) / 100_000))`
   (`CHUNK_ROWS = 100k`, `MAX_CHUNKS = 512`); checksum each chunk on both sides.
   Matching chunks are pruned; a dirty chunk ≤ `DIRECT_PULL_ROWS = 250k` goes
   straight to leaf-pull, a larger one to recursive bisection.
3. **Early bail**: once ≥ 8 chunks are evaluated, if the dirty fraction exceeds
   **`bail_pct = 0.60`**, raise `BailToStreaming` — scattered diffs make
   `P(bucket clean) ≈ 0.99^bucket ≈ 0`, so bucket pruning is futile and streaming
   is the honest path.
4. **Recursive bisection** of surviving dirty ranges (same `BRANCHING = 8` logic as
   recursive-hash), then **leaf detail** via `compare_rows`.

Cross-engine row hashes fold an MD5 of the canonical payload to a signed 32-bit int
identically on each dialect (sqlite UDF / Snowflake `MD5` / Oracle `STANDARD_HASH`).

### 7.7 Streaming window compare (`streaming_compare`)

The **correctness floor**: works for any column type at any scale without an
in-memory ceiling. Requires a single numeric key. Walk the key space in ordered
windows (`WHERE key > :last ORDER BY key LIMIT :window`), pulling the matching
target window and running `compare_rows` per window, merging outcomes under global
sample caps. Boundary duplicates of the window's max key are pulled fully so one
key's rows never split across windows; a target-only tail sweep catches keys past
the last source key. Window size adapts to `memory_gb` (default 50k, clamped
10k–500k). Used when fingerprint bails, when hashing is unreliable (encoding-suspect
columns), or when types preclude cross-engine canonicalization.

### 7.8 Spark (`spark_compare`) — opt-in

Retained for environments with a Spark cluster and very wide/large tables; selected
only when `engine == "spark"` and a Spark session is available (the default build
has no JVM dependency). Reads both sides via partitioned JDBC, applies the same
canonical normalization, and produces the same five buckets.

> **Planned — row-hash merge-join tier.** Benchmarks ([findings §6](../cross-engine-compare-findings.md))
> show that on *scattered* diffs, bailing from fingerprint to raw streaming is the
> one real performance weakness (moving all 30 columns of ~20M rows). A prototype
> (`scripts/rowhash_poc.py`) that moves only `(key, row_hash)` pairs and joins on
> them is **flat ~245–281s regardless of diff dispersion** (3.7–6.6× over
> streaming). The plan is to make row-hash the fingerprint **bail target** (instead
> of raw streaming), keeping fingerprint for low-dispersion/identical tables and
> raw streaming only as the floor for non-canonicalizable types.

## 8. Result structure (cell-level drill-down) (2c)

Every engine converges on the same shape. The lightweight **summary** always lives
in the metadata DB; the heavy **detail** (cell-level diffs + bad-record samples,
which contain real values) goes through the configurable **`ResultStore`** (§8.1).

```jsonc
CompareResult {
  job_id, kind, status,                       // succeeded | partial | failed | needs_decision
  key: [ "ORDER_ID" ],                        // detected or user-supplied
  compared_columns: [ ... ],
  summary: {
    src_row_count, dst_row_count,
    keys_total,
    matched_rows,
    value_difference_rows,                    // key matched 1:1, some cell differs
    src_only_rows, dst_only_rows,             // present on one side only
    ambiguous_rows,                           // many-to-many on a key
    match_rate_pct,
    per_column_mismatch: { "AMOUNT": 128, "STATUS": 4 },  // counts by column
    stopped_early: false,                     // true if fail-fast / budget tripped
    engine: "fingerprint"                     // which tier ran
  },
  differences: [                              // paged; full set in ResultStore
    {
      key: { "ORDER_ID": "10231" },
      status: "value_difference" | "source_only" | "target_only" | "ambiguous",
      cells: [
        { column: "AMOUNT", src: "100.00", dst: "100.50", canonical_type: "NUMERIC", differs: true },
        { column: "STATUS", src: "OPEN",   dst: "CLOSED",  canonical_type: "STRING",  differs: true }
      ]
    }
  ],
  warnings: [ ... ],                          // non-fatal (tolerance applied, gate notes, bail)
  detail_ref: "<ResultStore pointer>"         // object-store URI OR Snowflake table ref
}
```

- The UI renders a diff grid: filter by column, by status; click a row to see the
  full record side-by-side with differing cells highlighted — the "analyze at the
  cell level" requirement (2c).
- Large result sets: summary + first N pages served from the metadata DB; full
  detail persisted via the `ResultStore` and paged/queried on demand.

### 8.1 `ResultStore` backends (D9 — data residency)

Because cell diffs and samples contain **real customer data**, where they land is
a per-group configuration, not a hardcode:

| Backend | Detail stored as | Use when |
|---------|------------------|----------|
| **Object store** (default v1) | Parquet under `{group}/compares/{job}/` in S3/MinIO/local FS | Simple, cheap; acceptable for data to transit the platform. |
| **Snowflake-native** (roadmap, configurable) | Rows written to a results schema **in the customer's own Snowflake account** | **Data never leaves the customer's perimeter** — the preferred enterprise posture. The UI/API query these tables back on demand. |
| **Local filesystem** | Parquet/JSON on disk | Desktop/local mode. |

The engine writes through the `ResultStore` interface; switching a group from
object-store to Snowflake-native is a config change. Observability bad-record
samples ([04 §2.2](./04-observability.md#22-deep-analysis-for-a-table)) use the
same `ResultStore`, so the residency guarantee is uniform across both products.

## 9. Budget guards, fail-fast & resilience

Every engine shares one `CompareOptions` budget, so no compare runs unbounded:

- **Time budget** — `timeout_s` (default `DEFAULT_COMPARE_TIMEOUT_S = 1800`, i.e.
  30 min) → a `deadline_epoch` checked inside recursion/windows.
- **Query budget** — `max_queries` (default `DEFAULT_MAX_QUERIES = 5000`) caps the
  number of pushdown probes a recursive/fingerprint compare may issue.
- **Memory budget** — `memory_gb` sizes streaming windows and the in-memory vs
  streaming routing decision.
- **Cancellation** — `should_cancel(job_id)` is polled; a cancel raises
  `CompareCancelled` and unwinds cleanly.
- **Fail-fast** — when `fail_fast` is on and accumulated differences reach
  `max_diff_rows`, the engine stops and sets `stopped_early` (diff counts become a
  lower bound). *Note:* fail-fast helps most on the streaming path; on fingerprint
  the in-DB checksum has usually already run, and the sample gate answers "do they
  differ?" sooner — see findings §5.
- **Pushdown first**: counts, schema, and same-engine compares never pull rows.
- **Per-table continue-on-error** → `partial` status; one failure never sinks a
  batch.
- Every DB query is tagged `dvp:{group}:compare:{job}` for cost tracing.

### 9.1 Systematic-column skip guard (`needs_decision`)

When the sample gate pins ~all divergence on **1–3 columns** (a shifted
date/timezone/format) while the rest match, the engine **halts before the expensive
compare** rather than silently taking the slow scattered path. The job finishes
fast in status **`needs_decision`** with a payload:

```jsonc
{ columns: ["UPDATED_DT"], reason: "...",
  rerun_skip: { ignore_columns: ["UPDATED_DT"] },      // [Skip these & compare the rest (fast)]
  rerun_full: { acknowledge_systematic: true } }        // [Compare everything anyway (slow)]
```

Each choice re-runs via the existing `PATCH options` + `:run`/`:submit` endpoints;
`acknowledge_systematic` bypasses the guard. Rationale: those 1–3 columns are
exactly what makes every row's hash differ → scattered → streaming; skipping them
lets the rest prune fast.

## 10. SQL safety (`sql_guard`)

User-supplied filter predicates and raw `SELECT`/`WITH` sub-selects are validated
before they reach a connector, and again **after** `{{variable}}` substitution:

- String literals are scrubbed first (so content inside quotes can't trip the
  checks), then the fragment is rejected if it contains `;`, SQL comments
  (`--` `/* */` `#`), set operators (`UNION`/`INTERSECT`/`EXCEPT`), or any
  statement keyword (`DROP`/`DELETE`/`UPDATE`/`INSERT`/`ALTER`/`CREATE`/`MERGE`/
  `CALL`/`EXEC`/`PRAGMA`/…).
- Sub-selects must additionally be a single statement that leads with `SELECT` or
  `WITH`.

A violation raises `ValidationError` and fails the job up front.

## 11. Extensibility

The `DialectAdapter` per source encapsulates type mapping, quoting, pushdown
capability flags (`supports_except`, hashing primitive, partitioning), and
count/schema SQL. Adding Postgres/SQL Server/BigQuery = implement one adapter plus
its `sql_canon` renderings and a `supported()` check per hash engine; the
sample gate, in-memory comparator, and result structure are dialect-agnostic.
