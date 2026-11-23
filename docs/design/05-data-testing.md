# 05 — Data Testing (Compare Engine)

Covers requirement group 2 (a–d): count compare, schema compare, cell-level data
compare across Snowflake ↔ Oracle (and any combination), with automatic
compare-key and compare-column detection.

This is a clean-room redesign informed by the existing Scala `datadiff` engine
(`/Users/dhiraj/Documents/dataq/datadiff`). We keep the *algorithms* that proved
out (pushdown fast-paths, sample+source-validate key detection, hash+greedy
cell matching, type normalization) and drop the legacy request/response
contracts — this is a new project, done the right way.

## 1. Compare job model

```
compare_job
├── id, group_id, org_id, name
├── kind          = count | schema | data
├── source        { connection_id, database, schema, table, filter? }
├── target        { connection_id, database, schema, table, filter? }
├── options       { key?, columns?, column_mapping?, tolerance?, ignore_spaces?,
│                   null_equals_empty?, case_insensitive?, bad_record_limit,
│                   strategy_hint? }
├── status        = queued | running | succeeded | failed | partial
├── result_ref    → result document (Postgres summary + S3 detail)
└── created_by / timestamps
```

A job can compare a set of table pairs (batch), continuing past a per-table
failure and reporting `partial` (proven pattern from datadiff — one bad table
doesn't fail the whole run).

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
  Oracle `NUMBER(38,0)` and Snowflake `NUMBER(38,0)` (both → `DECIMAL(38,0)`) are
  recognized as equal, and `VARCHAR2` ↔ `VARCHAR` matches.
- Report per column: present-on-both / source-only / target-only; type match
  (raw + canonical); nullability; ordinal position; PK/index where the source DB
  exposes it (skipped for Snowflake, which lacks enforced constraints in
  metadata — same caveat datadiff handles).
- Result: list of `SchemaCompareRecord { column, src_type, dst_type,
  canonical_match, nullability_match, status }`.

## 4. Compare-key detection (2d)

Automatic detection of the join key, reusing datadiff's two-tier method:

1. **Keyable-column filter**: exclude `FLOAT`/`DOUBLE`/`TIMESTAMP` (precision /
   granularity make them unreliable join keys); keep `STRING`, integral types,
   `DATE`, `DECIMAL` (a `NUMBER(38,0)` surrogate id is a great key).
2. **Name-priority scoring**: rank candidate columns — `id` > `*_id`/`*_key` >
   `code`/`number` > others; penalize nullable columns.
3. **Sample-based greedy composite** (~10k rows pulled once): greedily add the
   highest-priority columns until the sampled combination is unique (or a column
   cap is hit). Optionally accelerate with `APPROX_COUNT_DISTINCT` (HyperLogLog)
   to rank candidates before exact checks.
4. **Source validation via pushdown**: confirm the proposed key on the *full*
   table with `SELECT COUNT(*) = COUNT(DISTINCT {key})` — a scalar, no data pull.
   Fall back to the next candidate if it fails.

The detected key (with a confidence and how it was validated) is returned to the
user, who can override it. Same key must be valid on both sides for a data
compare; if not, we report it clearly rather than producing garbage.

## 5. Compare-column detection (2d)

- **Default**: compare all columns present on both sides (the intersection),
  excluding the key columns.
- **Column mapping**: user can map differently-named columns (`SRC.CUST_ID` ↔
  `DST.CUSTOMER_ID`); only mapped columns are compared.
- **Compare-common-only** toggle: restrict to the intersection explicitly.
- **Per-side excludes**: drop audit/technical columns (`_LOADED_AT`, etc.).
- Auto-suggestion: columns with wildly different canonical types are flagged as
  "likely not comparable" and excluded by default with a warning.

## 6. Type normalization (the correctness core)

Before any value comparison, both sides are mapped to a **canonical type system**
and values normalized, so cross-DB representational differences don't cause false
mismatches:

| Canonical | Maps from | Comparison rule |
|-----------|-----------|-----------------|
| `NUMERIC` | Oracle `NUMBER`, Snowflake `NUMBER/DECIMAL/FLOAT`, int types | Compare as decimal/double; ignore precision/scale differences; optional absolute/relative tolerance. |
| `STRING` | `VARCHAR`, `VARCHAR2`, `CHAR`, `TEXT` | Optional `trim` (ignore trailing/leading spaces), optional case-insensitive. |
| `DATE` | `DATE`, Oracle `DATE` (has time) | Compare by calendar date / epoch millis. |
| `TIMESTAMP` | `TIMESTAMP*`, `TIMESTAMP_TZ/LTZ/NTZ` | Compare by epoch; normalize timezone to UTC. |
| `BOOLEAN` | `BOOLEAN`, `BIT`, `NUMBER(1)`/`CHAR(1)` flags | Coerce truthy/falsy representations. |

Null handling (configurable, datadiff-compatible defaults): `NULL`, the string
`"null"`, and empty string `""` are treated as equivalent when
`null_equals_empty=true` (default). These knobs are all per-job options.

## 7. Data compare — strategy selector (2c)

Like datadiff, we pick the cheapest correct strategy:

```
choose_strategy(src, dst, opts):
    if src.conn == dst.conn == SAME_SNOWFLAKE_ACCOUNT
       and no transform/tolerance/filter and types align 1:1:
          → SERVER_SIDE_EXCEPT          # metadata-speed, no Spark
    elif same_database_dialect and pushdown_supported (SF/PG/MSSQL):
          → PUSHDOWN_EXCEPT_JOIN        # SQL runs in the DB
    elif key_is_integer and adaptive_enabled:
          → SPARK_ADAPTIVE_RANGE        # segment by integer-key ranges
    else:
          → SPARK_HASH_GREEDY           # general cross-DB fallback (Oracle↔SF)
```

### 7.1 Server-side EXCEPT fast-path (same Snowflake account)
```sql
SELECT COUNT(*) FROM (SELECT <cols> FROM src EXCEPT SELECT <cols> FROM dst);  -- src-only
SELECT COUNT(*) FROM (SELECT <cols> FROM dst EXCEPT SELECT <cols> FROM src);  -- dst-only
```
If counts are 0/0 the tables match; otherwise pull a bounded sample of differing
rows for the UI. *Caveat (documented):* set semantics dedupe, so identical
DISTINCT rows with different duplicate multiplicity compare equal — we surface
this limitation and offer the key-based path when multiplicity matters.

### 7.2 Pushdown EXCEPT + OUTER JOIN (same dialect, different DBs of same engine)
SQL-side `EXCEPT` to find differing rows plus a keyed `FULL OUTER JOIN` /
`IS DISTINCT FROM` to attribute *which columns* differ. Runs in the DB; only
differing rows/samples return. (Oracle lacks `IS DISTINCT FROM`/`LIMIT` ergonomics
→ it falls through to Spark.)

### 7.3 Spark adaptive range (large integer-keyed tables)
Partition the key space into integer ranges via cheap metadata probes, compare
segment-by-segment, aggregate. Supports fail-fast, bounded bad-record samples,
and difference-tolerance %. Keeps memory bounded on huge tables.

### 7.4 Spark hash + greedy cell match (general cross-DB — the Oracle↔Snowflake path)

This is the workhorse for "compare Oracle vs Snowflake at the cell level":

1. **Read both sides** into Spark with JDBC partitioned reads (parallelized by the
   key range). Apply canonical type normalization (§6).
2. **Align schema**: reorder/rename to the column mapping; add missing columns as
   nulls.
3. **Key-group + hash dedup**: within each key, hash the compared columns; rows
   whose hashes match on both sides are exact matches and dropped cheaply.
4. **Greedy within-key matching** for the remainder: when a key has multiple rows
   (or fuzzy matches), compare all-to-all and greedily pair rows by number of
   matching columns; leftovers are `source-only` / `target-only`.
5. **Cell-level diff**: for each matched pair, record exactly which columns differ
   and both values (bounded to `bad_record_limit` samples per column to control
   size).

## 8. Result structure (cell-level drill-down) (2c)

Redesigned clean (no legacy contract). The lightweight **summary** always lives in
the metadata DB; the heavy **detail** (cell-level diffs + bad-record samples,
which contain real values) goes through the configurable **`ResultStore`** — see
D9.

```jsonc
CompareResult {
  job_id, kind, status,                       // succeeded | partial | failed
  key: [ "ORDER_ID" ],                        // detected or user-supplied
  compared_columns: [ ... ],
  summary: {
    src_row_count, dst_row_count,
    matched_rows,
    src_only_rows, dst_only_rows,             // present on one side only
    mismatched_rows,                          // key matched, some cell differs
    match_rate_pct,
    per_column_mismatch: { "AMOUNT": 128, "STATUS": 4 }   // counts by column
  },
  differences: [                              // paged; full set in S3
    {
      key: { "ORDER_ID": "10231" },
      status: "mismatch" | "src_only" | "dst_only",
      cells: [
        { column: "AMOUNT", src: "100.00", dst: "100.50", canonical_type: "NUMERIC" },
        { column: "STATUS", src: "OPEN",   dst: "CLOSED" }
      ]
    }
  ],
  warnings: [ ... ],                          // non-fatal (e.g. tolerance applied)
  detail_ref: "<ResultStore pointer>"         // object-store URI OR Snowflake table ref
}
```

- The UI renders a diff grid: filter by column, by status; click a row to see the
  full record side-by-side with differing cells highlighted — exactly the
  "analyze at the cell level" requirement (2c).
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

## 9. Performance & resilience

- **Pushdown first**: counts, schema, and same-engine compares never pull rows.
- **Partitioned JDBC reads** and **repartitioning** for wide/large tables in Spark.
- **Shuffle-partition tuning** based on estimated row counts.
- **Bounded samples** (`bad_record_limit`) so results stay small.
- **Per-table continue-on-error** → `partial` status; one failure never sinks a
  batch.
- **Fail-fast** option for quick "are these even close?" checks.
- Every DB query tagged `dvp:{group}:compare:{job}` for cost tracing.

## 10. Extensibility

The `DialectAdapter` per source encapsulates type mapping, quoting, pushdown
capability flags (`supports_except`, `supports_is_distinct_from`, partitioning),
and count/schema SQL. Adding Postgres/SQL Server/BigQuery = implement one adapter;
the strategy selector and Spark paths are dialect-agnostic.
