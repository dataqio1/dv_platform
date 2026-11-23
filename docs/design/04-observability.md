# 04 — Data Observability

Covers requirement group 1 (a–i): monitors as first-class objects, table search &
deep analysis, DQ rule CRUD, three-level auto-generation, runtime rule editing,
incremental (delta-only) analysis, and dashboards.

## 1. The Monitor (first-class citizen) — (1a, 1b)

A **Monitor** watches one or more tables in a Snowflake schema and owns
everything about their quality over time.

```
Monitor
├── id, group_id, org_id, name, description, tags[]
├── connection_id                       → which Snowflake connection
├── scope: { database, schema, tables[] }   (one or many tables; wildcards allowed)
├── schedule (cron)  + timezone
├── watermark_config (per table)        → how we find "new data" (§4)
├── dq_rules[]  (versioned)             → the assertions (§3)
├── status: active | paused
├── runs[]      (history)
└── incidents[] (open/resolved)
```

Being "first class" means:

- It has a stable ID, a URL, an owner (group), an audit trail, an API, an MCP
  resource, permissions, and a lifecycle (create → run → pause → archive).
- Rules, runs, metrics, incidents, and dashboards all hang off the monitor.
- A monitor over multiple tables fans out to per-table analysis but rolls up to
  one monitor-level health status.

### Monitor of multiple tables
`scope.tables` can be an explicit list or a pattern (`SALES.PUBLIC.ORDERS_*`).
Each table is analyzed independently (its own watermark, its own rules) but the
monitor presents a unified health view and a single schedule.

## 2. Table search & deep analysis — (1c)

### 2.1 Search

- Users search across tables that are **within their group's connections'
  allowed scope** (never the whole account beyond what's permitted).
- Backed by a catalog table populated from Snowflake `INFORMATION_SCHEMA` /
  `ACCOUNT_USAGE` (tables, columns, row counts, last-altered), refreshed on a
  schedule and on-demand. Full-text search on table/column names + tags.
- A table may be *monitored* or *unmonitored*; search surfaces both and offers
  "Create monitor" for unmonitored ones.

### 2.2 Deep analysis for a table

A per-table analysis page shows:

- **Profile**: per column — type, null rate, distinct count/cardinality, min/max,
  mean/stddev (numeric), min/max length (string), top-N frequent values,
  approximate histograms.
- **Freshness**: last load time, load cadence, rows added per run.
- **Volume**: row count trend, delta size per run.
- **Schema history**: added/removed/retyped columns over time (drift).
- **Rule results**: current pass/fail per rule + trend.
- **Incidents**: open & historical.
- **Sample bad records** for failing rules (bounded sample). These contain real
  data values, so they are written via the configurable **`ResultStore`** — object
  store by default, or **the customer's own Snowflake** when configured, so values
  never leave their account (D9; see [05 §8](./05-data-testing.md#8-result-structure-cell-level-drill-down-2c)).

Profiling runs as **pushdown aggregate SQL** in Snowflake (see
[01](./01-architecture.md#compute-model)) — one templated query computes most
column stats in a single pass over the delta partition.

## 3. DQ rules — CRUD + auto-gen + runtime edit (1d, 1e, 1f)

### 3.1 Rule model

Rules are declarative, versioned, and stored as JSONB config so we can add rule
types without schema migrations.

```
dq_rule
├── id, monitor_id, table_ref, group_id
├── name, description
├── type            = one of the catalog below
├── column(s)       (nullable for table-level rules)
├── config (JSONB)  { threshold, operator, allowed_values, regex, tolerance, ... }
├── severity        = info | warning | critical
├── scope           = incremental | full        (see §4; default incremental)
├── enabled         = bool
├── version         (bumped on every edit)
└── created_by / updated_by / timestamps
```

### 3.2 Rule catalog

| Category | Example rule types |
|----------|-------------------|
| **Completeness** | `not_null`, `null_rate < x%`, `blank_rate`, `row_count > 0`, `row_count within ±x% of trend` |
| **Uniqueness** | `unique(col)`, `unique(col_a, col_b)` (composite), `duplicate_rate < x%` |
| **Validity** | `in_set(allowed_values)`, `regex_match`, `data_type`, `length between`, `numeric range min/max`, `date not in future` |
| **Consistency** | cross-column (`end_date >= start_date`), conditional (`if status='X' then amount>0`) |
| **Distribution / anomaly** | mean/stddev/quantile within expected band; categorical distribution drift (PSI); volume anomaly |
| **Freshness** | `max(load_ts)` within N hours of now; expected cadence |
| **Schema** | no unexpected column add/drop/retype (schema drift) |
| **Referential** | FK-style: values exist in a reference table |
| **Custom SQL** | user-supplied boolean SQL expression evaluated in Snowflake |

Each rule type is a class implementing:
```python
class Rule(Protocol):
    def to_sql(self, table, partition_predicate) -> SqlFragment: ...  # aggregate expr
    def evaluate(self, aggregates) -> RuleResult: ...                 # pass/fail + metric
```
So the profiler can **batch many rules into one Snowflake query** (all the
`to_sql()` fragments combined into a single `SELECT`), then evaluate each in
Python. This keeps warehouse cost low regardless of rule count.

### 3.3 CRUD (1d)

Full REST CRUD at the table level (see [08](./08-api.md)):
`POST/GET/PATCH/DELETE /monitors/{id}/rules`. Editing bumps `version` and writes
an audit entry. Deleting is a soft-delete (retains history for past runs).

### 3.4 Auto-generation with 3 levels (1e)

`POST /monitors/{id}/rules:autogenerate { level: 1|2|3, tables?: [...] }`

The generator first **profiles** the table(s), then proposes rules calibrated to
the observed data. The three levels are cumulative:

| Level | Name | Generated rules |
|-------|------|-----------------|
| **1** | **Essential** | `row_count > 0`; `not_null` on columns observed 100% non-null; freshness on the detected load-timestamp column; schema-drift guard. Low noise, high signal. |
| **2** | **Standard** (L1 +) | `null_rate < observed+margin` on partially-null cols; `unique` on detected key columns; `in_set` for low-cardinality categoricals; numeric `min/max range` from observed p1–p99; `length` bounds for strings; data-type checks. |
| **3** | **Comprehensive** (L2 +) | distribution/anomaly bands (mean/stddev/quantile, PSI drift); cross-column consistency inferred from correlations; regex/pattern inference (emails, phones, codes); referential candidates; volume-anomaly detection. |

- Every generated rule is created **disabled or in "suggested" state** by default
  with a confidence score, so a human reviews before it can raise incidents
  (configurable: auto-enable L1).
- Thresholds are derived from the profile (e.g. `null_rate` threshold = observed
  rate + configurable margin) so they fit the actual data, not arbitrary defaults.
- Re-running auto-gen proposes *new* rules and flags drifted thresholds; it never
  silently overwrites a human-edited rule.

Key detection for uniqueness rules reuses the proven **sample + source-validate**
approach from datadiff: sample ~10k rows to greedily propose a candidate key
(prioritizing `id`/`_key`/`code` columns, excluding floats/timestamps), then
confirm with a pushed-down `COUNT(DISTINCT …)` on the full table. See
[05 §4](./05-data-testing.md#4-compare-key-detection).

### 3.5 Runtime rule editing (1f)

- A **monitor run executes an immutable snapshot** of the rule set (captured at
  run start, referenced by rule versions). This means:
  - Editing a rule while a run is in flight is safe — the in-flight run finishes
    with the old snapshot; the change applies to the **next** run.
  - No run is ever stopped or corrupted by an edit.
- For **long-running / streaming-style** monitors, edits are picked up at the next
  batch/micro-partition boundary, and the run record notes "ruleset vX → vY at
  partition N" so results remain interpretable.
- Every edit is versioned and audited; you can always see which rule version
  produced a given historical result.

## 4. Incremental analysis — only new data (1g)

> Requirement: *"analyze only the new data and not the old data we already
> analyzed. Only exception is unique constraints."*

### 4.1 Scheduling

Monitors run on a **configurable cron** (hourly or daily are the common choices);
there is **no real-time requirement** (D10). Each scheduled firing is one
incremental run per table. This keeps the design simple and cheap: we favor a
pull-on-schedule high-water-mark model over always-on change streams.

### 4.2 Watermark strategy

Each monitored table has a **watermark**: a column + last-processed value marking
how far we've analyzed. Sources of the watermark, in preference order:

1. **High-water-mark column (primary):** a monotonically increasing column —
   load/ingest timestamp, `_LOADED_AT`, an auto-increment/sequence id, or Snowflake
   `METADATA$…`. Run predicate: `WHERE {wm_col} > :last_value`. This is the default
   because it needs no extra Snowflake objects and fits the hourly/daily cadence.
2. **Snowflake Streams (optional):** for tables where a stream already exists or the
   user opts in, consume only changed rows since the last offset — Snowflake tracks
   the delta natively. Useful for update-heavy tables where a HWM column is absent.
3. **`CHANGES` clause / Time Travel:** `SELECT … FROM t CHANGES(INFORMATION => …)
   AT (TIMESTAMP => :last_run_ts)` as a middle ground when no HWM column and no
   stream exists.
4. **Manual / full (last resort):** if no delta signal exists, the user can opt a
   table into full-scan (with a cost warning) or provide a partition expression.

The watermark column is auto-detected during monitor creation (look for
timestamp/sequence columns named `*_at`, `load*`, `updated*`, `ingest*`; confirm
monotonicity from the profile) and is overridable.

### 4.3 Run algorithm

```
run(monitor, table):
    wm      = load_watermark(table)                 # last processed value
    pred    = build_partition_predicate(wm)         # e.g. LOAD_TS > '2026-09-30 12:00'
    ruleset = snapshot_enabled_rules(table)         # immutable for this run

    # split rules: incremental vs must-scan-full
    inc_rules  = [r for r in ruleset if r.scope == INCREMENTAL]
    full_rules = [r for r in ruleset if r.scope == FULL or r.type == UNIQUENESS]

    # ONE pushdown query for the delta partition
    agg_delta  = snowflake.execute(build_agg_sql(inc_rules, table, pred))
    results    = [r.evaluate(agg_delta) for r in inc_rules]

    # uniqueness / referential must look at more than the delta (see 4.4)
    for r in full_rules:
        results += evaluate_full_scope(r, table, wm)

    persist(metrics, results, incidents)
    advance_watermark(table, new_high_value(agg_delta))   # atomic, only on success
```

- The watermark advances **only if the run succeeds**, so a failed run re-reads
  the same delta next time (at-least-once, no gaps).
- `new_high_value` is computed from the max watermark-column value actually seen,
  not "now", to avoid skipping rows that arrive during the run.

### 4.4 The uniqueness exception (1g)

Uniqueness can't be judged on the delta alone — a new row may duplicate an *old*
row. So uniqueness (and referential integrity, which is similar) is handled
specially, pushed down to Snowflake so we still don't pull data:

- **Efficient full-table check via pushdown**, not a Spark full pull:
  ```sql
  SELECT COUNT(*) AS dup_groups
  FROM (SELECT {key_cols} FROM {table} GROUP BY {key_cols} HAVING COUNT(*) > 1);
  ```
  or `COUNT(*) - COUNT(DISTINCT {key_cols})`. Only a scalar returns.
- **Optimization**: for append-only tables we can restrict the uniqueness check to
  "do any delta keys collide with existing keys" using a semi-join of the delta
  against the table, which is far cheaper than a global group-by. Chosen
  automatically based on table characteristics.
- This is the *documented exception* to delta-only: uniqueness rules read beyond
  the new data by design, and we make that explicit in the run record.

### 4.5 Backfill & reset

- A user can reset a watermark (re-analyze from a point in time) or run a bounded
  backfill; both are explicit, audited actions with cost estimates.

## 5. Incidents & alerting

- A failing rule opens/updates an **Incident** (dedup by rule + table + open
  window) with severity, first/last seen, sample bad records, and trend.
- Lifecycle: open → acknowledged → resolved (auto-resolves when the rule passes
  again for N consecutive runs).
- Alerting via pluggable channels (email, Slack, Teams, webhook). Consistent with
  golden rule #3: if an alert channel fails, the run still succeeds but the
  failure is surfaced as a **warning** on the run — never silently dropped.

## 6. Charts & dashboards (1h)

- **Monitor dashboard**: health score, rule pass-rate over time, open incidents,
  freshness/volume trends, per-table roll-up.
- **Table analysis**: the profile + trends described in §2.2, with per-column
  drill-down and sparklines.
- **Rule detail**: metric value vs threshold over time, breach markers, sample
  offenders.
- **Group overview**: all monitors' health, incident feed, worst-offender tables,
  SLA/coverage (what % of important tables are monitored).
- Charts are rendered from time-series metrics stored per run (see
  [07](./07-data-model.md)); the UI uses ECharts/Recharts. All data is fetched via
  the same REST API the MCP server uses, so agents get the same numbers.

## 7. Data residency for samples (resolved)

The truncated "1i" line in the original brief was confirmed a typo and dropped.
The one data-sensitivity concern it might have implied is addressed by D9: all
sample/profile artifacts that contain real values go through the configurable
**`ResultStore`**, which can keep them inside the customer's own Snowflake. See
[§2.2](#22-deep-analysis-for-a-table), [§5](#5-incidents--alerting), and
[05 §8](./05-data-testing.md#8-result-structure-cell-level-drill-down-2c).
