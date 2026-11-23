# 11 — Load-Type Detection & Load-Aware DQ Rules

> Status: **Draft for review.** Extends [04 — Data Observability](./04-observability.md),
> [07 — Data Model](./07-data-model.md), and [10 — Production Data Observability](./10-production-data-observability.md).
> Builds on what already ships: the per-table load-pattern catalog (`services/table_metadata.py`),
> watermark detection (`observability/watermark.py`), the pushdown rule evaluator
> (`observability/evaluator.py`), and the historical-baseline metric engine
> (`observability/metrics.py` + `baseline.py`).

## 0. TL;DR

The load type of a table (truncate-and-load, append-only, CDC, **SCD Type 2**, partitioned,
reference) determines what "correct" even means for it. Today we **store** a table's load
pattern and its role columns but **nothing uses them to detect issues** — every table is judged
by *total row count*, which is nearly useless for SCD2 (the total only ever grows).

A second realisation shapes this design: **every ETL tool stamps the same handful of
audit/tracking columns into every table** — a load timestamp, an update timestamp, a batch id, a
source-system tag, sometimes effective-from/to + a current flag. If we capture those **once, as the
first step of creating a monitor**, they power watermark, freshness, batch tracking, load-type
detection, SCD2 semantics, and source lineage — across *all* the monitor's tables, for free. The
customer's `src_eff_start_date` / `src_eff_end_date` / `batch_id` is just **one tool's convention**;
dbt, Fivetran, Airbyte, Stitch, Informatica, ADF and hand-rolled warehouses each have their own.

This doc specifies:

1. **ETL metadata columns** (§3): declare the audit/tracking columns once per monitor (via a
   tool **preset**, auto-detect, or by hand); they seed everything downstream and are per-table
   overridable.
2. A richer **load-profile config** (§4) that can express the real SCD2 the field uses —
   effective-from/to with a **high/max-future-date = active** convention — plus the business key
   and batch id. The user declares it; detection proposes it; the user can edit it.
3. Turning load-type detection into a **load-aware DQ rule pack** (§6): assertions proposed
   *because of how a table is loaded*, each with a **provenance label** (automated / suggested /
   user / injected) and an individual **enable/disable** toggle.
4. Concrete detection for the four SCD2 checks, their pushdown SQL, the API, the UX, the migration,
   and the real-data test matrix.

---

## 1. Motivation — why this is the #1 correctness gap

### The customer scenario (one tool's convention, not the only one)
A curated dimension is loaded **SCD Type 2**:

| column | meaning |
|---|---|
| `src_eff_start_date` | when this version became effective |
| `src_eff_end_date` | when it stopped being effective — **a max-future date (e.g. `9999-12-31`) means the row is still active**; a past date means it is closed |
| `batch_id` | the ETL batch that wrote the row |
| *(business key, e.g. `customer_id`)* | the natural entity the versions belong to |

Those column *names* are this customer's ETL tool's convention. The same roles appear everywhere
under different names (§3.2) — dbt snapshots call them `dbt_valid_from` / `dbt_valid_to`, Fivetran
stamps `_fivetran_synced` / `_fivetran_deleted`, and so on. The design must key off **roles**, not
literal names.

What can go wrong in SCD2 is invisible to total-row-count monitoring:

- The load **stops closing old versions** → two or more rows for the same `customer_id` have a
  max-future end date at once (overlapping active versions). Total count still grows.
- A bad load **drops or wrongly closes active rows** → the **active population** falls, even
  though the historical total is unchanged or still rising.
- The job **doesn't run** → no new batch id, no new effective-from, but a plain row-count freshness
  check sees nothing because the table wasn't truncated.
- **Gaps/overlaps** in the `[start, end)` intervals per key → history is no longer reconstructable.

### What exists vs. what's missing
- `TableMetadata` already has `effective_from_column`, `current_flag_column`, `changed_at_column`
  (see `db/models.py`), and the config UI renders whatever the backend spec declares
  (`TableLoadConfig.tsx` is fully data-driven from `pattern_specs()`).
- **Gap A — no metadata-column concept:** there's no way to declare, once, "this source's load
  timestamp is `dw_load_ts`, batch is `dw_batch_id`, …". Every table is configured from scratch.
- **Gap B — model:** there is no `effective_to` role, no "active definition" (flag value *or*
  high-date sentinel), no business key, no batch-id role. The *end-date* flavour of SCD2
  (the common one) cannot be expressed at all.
- **Gap C — detection does nothing:** the role columns are stored but read by **no** evaluator.
  The engine only ever measures total `COUNT(*)`. (Verified: grep for `effective_from_column` /
  `current_flag_column` outside `table_metadata.py` → only storage, no logic.)
- **Gap D — classifier is exact-match:** `classify_pattern()` matches `start_date` literally, so
  `src_eff_start_date` is missed; `batch_id` isn't recognised.

---

## 2. Load-type taxonomy (unchanged set, sharper SCD2)

Keep the seven patterns in `LOAD_PATTERNS`. The only structural change is that **SCD2 gains two
sub-shapes** that share one *active predicate*:

| pattern | how "active / latest" is defined |
|---|---|
| `full_snapshot` (Truncate & Load) | whole table replaced each run; row count legitimately swings |
| `append` | insert-only; watermark = append timestamp |
| `cdc` | insert + update; watermark = change timestamp |
| **`scd2`** | versioned history; **active** defined by *either* a current-flag *or* an end-date sentinel (§4.2) |
| `partitioned` | loaded per partition (`dt`/`ds`) |
| `reference` | small slowly-changing lookup |
| `unknown` | not classified → safe defaults only |

> "Curated SCD2" in the request is just `scd2` applied to a gold/curated-layer table. No new
> pattern needed — it is distinguished by its `layer`, not its load type.

---

## 3. ETL metadata columns — declare once (the first step)

### 3.1 Premise
Pipelines stamp a small, stable set of **audit/tracking columns** into (nearly) every table they
produce. They are the raw material for observability: a load timestamp *is* freshness, a batch id
*is* "did the run happen", a current flag *is* the active population. Rather than re-detect these
per table, we ask for them **once at monitor creation (step 0)** and apply them across every table
in scope — per-table overridable, and still auto-detectable where a table deviates.

### 3.2 The metadata-column roles (tool-agnostic)
A **role → column** mapping. Every ETL tool fills some subset; names differ, roles don't.

| role | drives | typical names |
|---|---|---|
| `loaded_at` | freshness, watermark (append) | `loaded_at`, `dw_load_ts`, `_loaded_at`, `_fivetran_synced`, `_airbyte_extracted_at`, `_sdc_received_at` |
| `updated_at` / `changed_at` | CDC watermark, freshness | `updated_at`, `dw_update_ts`, `dbt_updated_at`, `last_modified` |
| `created_at` | append watermark | `created_at`, `inserted_at`, `record_created_at` |
| `batch_id` | "did the load run", batch freshness, run grouping | `batch_id`, `dw_batch_id`, `_sdc_batched_at`, `load_id` |
| `source_system` | lineage/grouping, multi-source dedup | `source_system`, `src_system`, `_sdc_source` |
| `job_run_id` | link incidents to an ETL run | `etl_job_id`, `run_id`, `_airbyte_raw_id` |
| `soft_delete_flag` | **exclude deleted rows** from counts/active | `_fivetran_deleted`, `_sdc_deleted_at`, `is_deleted`, `deleted_flag` |
| `record_hash` | change detection, dedup | `_fivetran_synced`-paired hash, `dbt_scd_id`, `row_hash` |
| `effective_from` | SCD2 version start | `src_eff_start_date`, `dbt_valid_from`, `valid_from` |
| `effective_to` | SCD2 version end / active sentinel | `src_eff_end_date`, `dbt_valid_to`, `valid_to` |
| `current_flag` | SCD2 active flag | `is_current`, `current_flag`, `is_active` |
| `business_key` | SCD2 integrity, dedup | natural key column(s) |

### 3.3 Presets (pick your tool, we prefill)
A small, extensible **preset library** maps a tool to the roles above, so the common case is one
click. `custom` lets the user type names; `auto` infers from the selected tables (§5).

```jsonc
// shipped presets (extensible — one place, both API + UI pick them up)
{
  "dbt_snapshot": { "effective_from": "dbt_valid_from", "effective_to": "dbt_valid_to",
                    "current_flag": null, "updated_at": "dbt_updated_at",
                    "record_hash": "dbt_scd_id", "scd2": { "open_end_is_null": true } },
  "fivetran":     { "loaded_at": "_fivetran_synced", "soft_delete_flag": "_fivetran_deleted" },
  "airbyte":      { "loaded_at": "_airbyte_extracted_at", "job_run_id": "_airbyte_raw_id" },
  "stitch":       { "loaded_at": "_sdc_received_at", "batch_id": "_sdc_batched_at",
                    "soft_delete_flag": "_sdc_deleted_at", "source_system": "_sdc_source" },
  "informatica_dw": { "loaded_at": "dw_load_ts", "updated_at": "dw_update_ts",
                      "batch_id": "dw_batch_id", "source_system": "src_system" },
  "custom":       {}   // the user declares names; or start from the closest preset and tweak
}
```

> `dbt_snapshot` is literally the SCD2 case from §1 under dbt's names — `dbt_valid_to IS NULL`
> means active (`open_end_is_null: true`). The customer's `src_eff_*`/`batch_id` is the
> `informatica_dw`-style preset. Same roles, different labels.

### 3.4 Where it lives & how it resolves
- **Named, reusable conventions (org library).** A convention is a first-class, org-scoped entity
  — `MetadataConvention {name, roles, scd2}` — e.g. **"IDMC-CDC"** or **"S3-Loader"**. The shipped
  presets (§3.3) are **system** conventions (served from code with `sys:<key>` ids); users save
  their own. Define a column mapping **once** and reuse it everywhere; edit it in one place and
  every connection that references it updates.
- **Connections attach one *or more* conventions** (`Connection.metadata_convention_ids`). Ten
  Snowflake connections where eight share one ETL tool all reference the same `IDMC-CDC`
  convention — no copy-paste, one edit updates all eight. A connection whose schema is populated by
  **two tools** (e.g. IDMC doing CDC + a separate S3→Snowflake loader) attaches **both**
  conventions. Monitors inherit the connection's set (`Monitor.metadata_convention_ids`); a monitor
  or table may still override.
- Backward-compatible: the earlier inline `Connection.metadata_columns`/`Monitor.metadata_columns`
  (a single anonymous convention) is still honoured, treated as one more attached convention.

#### Resolving multiple conventions per table — coverage-based (the two-tool case)
When more than one convention is attached, each **table** picks the one whose declared columns
actually exist in it (an extension of §3.5's advisory rule from one convention to many):
- For each attached convention, compute **coverage** = how many of its roles' columns are present
  in the table.
- The **highest-coverage** convention wins for that table; ties fall back to attachment order.
  A table matching none → auto-detect → `unknown` (never forced).
- So in a mixed `IDMC-CDC` + `S3-Loader` connection, a table carrying IDMC's `AUDIT_UPDATE_TS`
  resolves as CDC under IDMC, while a table carrying `_FILE_BATCH` / `_LOADED_AT` resolves under
  S3-Loader — automatically, with no table-name rules to maintain. The per-table view shows which
  convention matched (and lets the user override).
- On monitor create/edit, the per-table **winning** convention is **projected onto each table's
  `TableMetadata`** (filling `watermark_column`, `effective_from_column`, `effective_to_column`,
  `batch_id_column`, etc.) so the rest of the system keeps reading per-table metadata as it does today.
- **Resolution precedence**: explicit per-table override → **best-covering attached convention**
  (monitor's set, inherited from the connection) → auto-detect → `unknown`.

### 3.5 The convention is advisory — not every table has these columns
A warehouse-wide default **must not assume every table conforms**. Staging/raw tables, third-party
imports, views, and legacy tables often have *some* of the audit columns, or *none*. The convention
is a **hint applied per-table only where the column actually exists**, never a requirement:

- **Per-table coverage check.** For each selected table, confirm which declared columns are really
  present (reuse `watermark.validate_watermark()` / schema introspection). Only present columns are
  projected onto that table's `TableMetadata`; absent ones are left unset.
- **Graceful per-table fallback.** A table missing a declared role falls through the precedence
  chain for *that role*: convention → auto-detect (heuristics, §5) → safe default. A table with
  **no** metadata columns at all just lands on auto-detect, then `full_snapshot`/`unknown` with
  conservative pillars — exactly as an unconfigured table behaves today. No errors, no blocked
  monitor.
- **Rules only reference columns that exist.** The load-aware rule pack (§6) is generated from the
  *resolved* per-table roles, so a freshness/active/SCD2 rule is never emitted against a column a
  table doesn't have. Partial coverage ⇒ a partial (still useful) pack.
- **Surface it, don't hide it.** A per-table **coverage readout** ("`dw_load_ts` found in 18/18,
  `dw_batch_id` in 16/18; 2 tables have no audit columns → auto-detected") is shown at setup and
  carried as `ApiResponse` warnings — never a silent skip (golden rule #3).

### 3.6 What it powers (one input, many pillars)
- **Freshness/watermark** ← `loaded_at` (or `updated_at`, or `batch_id` for SCD2, §6.4).
- **Load-type detection** ← presence of `effective_from`+`effective_to`/`current_flag` ⇒ `scd2`;
  `updated_at` ⇒ `cdc`; `created_at`/`loaded_at` only ⇒ `append` (§5).
- **Volume (active)** ← `current_flag`/`effective_to` define the active predicate (§4.2).
- **Correct counts** ← `soft_delete_flag` excludes deleted rows from volume/active everywhere.
- **Lineage/grouping** ← `source_system`, `job_run_id` tie datasets and incidents to a source/run.

---

## 4. Load-profile config model (the user can declare this)

### 4.1 New role columns on `TableMetadata`
Add first-class roles, consistent with the existing ones (new idempotent Alembic migration —
`migrations/00NN_scd2_roles.py`; never edit an existing migration, per the golden rules):

```
effective_to_column    str|null   # e.g. src_eff_end_date / dbt_valid_to
business_key           JSON list  # e.g. ["customer_id"]
batch_id_column        str|null   # e.g. batch_id / dw_batch_id
```

`effective_from_column`, `current_flag_column`, `changed_at_column`, `watermark_column`,
`partition_column`, `freshness_sla_seconds` already exist and are reused as-is. These are what the
§3 convention projects into.

### 4.2 The *active predicate* (stored in `config`, resolved once)
The semantics that make SCD2 detectable live in the extensible `config` JSON (no migration churn
for tuning knobs), computed into a single reusable SQL predicate:

```jsonc
// TableMetadata.config.scd2
{
  "active_mode": "end_date" | "flag",        // how to tell active from closed
  "high_date_value": "9999-12-31",            // the max-future sentinel (end_date mode)
  "open_end_is_null": true,                    // treat NULL end_date as open too (dbt-style)
  "current_flag_value": "Y"                    // what the flag holds for active (flag mode)
}
```

Resolution (pure function `scd2_active_predicate(meta) -> sql`):

- **flag mode:** `"<current_flag_column>" = <current_flag_value>`
- **end-date mode:** `("<effective_to_column>" >= <high_date_value>` *(or `IS NULL` when
  `open_end_is_null`)*`)`
- If a `soft_delete_flag` role is set, it is AND-ed in everywhere (active rows are never deleted).

This predicate is the keystone — every SCD2 check reuses it, and the UI prints it back to the
user in plain language so there is no ambiguity about what "active" means (§8).

### 4.3 Provenance of the config
Unchanged from today: `TableMetadata.source` records who set it (`user` | `auto` | an external
ingestion system's name) and `confidence` for auto-classified records. Per-table overrides the
monitor default (`resolve()`); injection via `PUT /table-metadata` stays the integration surface.

---

## 5. Detection (auto-classify) improvements

`classify_pattern()` becomes name-tolerant and SCD2-end-date aware, and it **starts from the §3
convention** when one is set (preset names first, heuristics only to fill gaps). It stays a
**suggestion** — never silently authoritative — returns a confidence and the roles, user confirms.

1. **Convention-first:** if the monitor's metadata-column map resolves roles for a table, use them
   directly (highest confidence); only fall back to heuristics for unmapped roles.
2. **Substring/suffix matching** (reuse the approach in `watermark._name_score`, which already
   does `p in low`): `src_eff_start_date` → `effective_from`; `src_eff_end_date` → `effective_to`;
   `batch_id` / `load_batch` → `batch_id`. A small ranked synonym table per role.
3. **End-date SCD2:** a start-ish **and** an end-ish date column ⇒ `scd2`, `active_mode="end_date"`.
   Flag SCD2 (`effective_from` + `is_current`) ⇒ `active_mode="flag"`. `dbt_valid_to` ⇒
   `open_end_is_null=true`.
4. **High-date sentinel discovery (sample-backed):** take the **dominant max value** of the
   end-date column from the sample (`observability/sample_profile.py` already pulls one); if one
   max-future value dominates the open rows, propose it as `high_date_value`, else default
   `9999-12-31` at low confidence for the user to set.
5. **Business key:** reuse the compare engine's key detector (`testing_engine/keydetect.py`) over
   the sample to propose `business_key`; always user-confirmable.

Everything remains editable in the UI and overridable by injection.

---

## 6. Load-aware DQ rules (the core of this change)

### 6.1 Concept — a "rule pack" generated from the load type
When a table's load type is detected/confirmed, the system proposes a **pack of assertions that
make sense for that load type**. The user reviews the pack and **enables/disables** each rule.
This generalises today's per-pattern `RULE_SUGGESTIONS` (which only produced total-row-count
baseline rules) to the full set of checks below, and spans **both** assertion engines:

- **Point-in-time assertions** → `DQRule` (evaluated by `evaluator.py`, pushdown aggregate SQL).
- **Historical-baseline assertions** → `MetricDefinition` + `MetricRule` (deviation / monotonic /
  growth in `metrics.py` + `baseline.py`).

### 6.2 Provenance labels + enable/disable (explicit requirement)
`DQRule.state` already encodes `suggested | enabled | disabled` — that **is** the enable/disable
and "not yet accepted" lifecycle. What's missing is *who/what authored the rule*. Add a provenance
field to both `DQRule` and `MetricRule` (migration):

```
origin  str   # one of:
              #   system      — generated automatically from the detected load type
              #   suggested   — profiler/level-based suggestion awaiting acceptance
              #   user        — created or edited by a person
              #   injected    — pushed in by an external ingestion/governance system
```

Label semantics in the UI (§8): a chip next to each rule — **Automated** (`system`),
**Suggested** (`suggested`), **User** (`user`), **Injected** (`injected`). `state` drives the
toggle (enabled/disabled); `origin` is the label; `confidence` shown for non-user rules. When a
user edits a `system`/`suggested` rule it flips to `origin=user` (so automation never silently
overwrites a human decision — golden-rule-aligned). Re-running detection never disables or edits a
rule a user has touched; it only adds new `system` rules in `suggested` state.

### 6.3 The SCD2 pack (the four checks)

| # | Check | Engine | Rule | Default | Why |
|---|---|---|---|---|---|
| 1 | **Active-record count baseline** | Metric + baseline | `MetricDefinition` (`COUNT(*) WHERE <active>`) + `MetricRule` deviation/monotonic | enabled | Catches a dropped/stalled active population — invisible to total-row-count today |
| 2 | **One active version per key** | Point-in-time | new `DQRule` type `scd2_active_unique` | enabled (severity=fail) | Overlapping open versions = a correctness bug |
| 3 | **Batch / effective-from freshness** | Point-in-time | `FRESHNESS` rule, source = `batch_id` or `effective_from` | enabled | "Did the load run?" for a table that is never truncated |
| 4 | **No-overlap / gap closure** | Point-in-time | new `DQRule` type `scd2_no_overlap` | **suggested** (heaviest query) | Intervals per key must not overlap or gap |

Packs for the other patterns stay as today (append → monotonic non-decreasing + growth band;
full_snapshot/reference → tight deviation; etc.) but now also flow through the provenance/enable
model.

### 6.4 Evaluation — pushdown SQL shapes
Let `A` = the resolved active predicate (§4.2), `K` = `business_key`, `S` = `effective_from_column`,
`E` = `effective_to_column`, `B` = `batch_id_column`.

**1. Active count** (metric source SQL):
```sql
SELECT COUNT(*) AS value FROM {table} WHERE {A};
```
Baselined by an existing `MetricRule` (deviation ±X% / monotonic). Reuses `metrics.py` unchanged.

**2. Active uniqueness per key** (`scd2_active_unique`, fails if any key has >1 open row):
```sql
SELECT COUNT(*) AS violations FROM (
  SELECT {K} FROM {table} WHERE {A} GROUP BY {K} HAVING COUNT(*) > 1
) d;
```
`passed = violations == 0`. Evidence: the sample query returns the offending keys (reuses the
evaluator's existing failing-rows sample path).

**3. Freshness from batch/effective-from** (extends `FRESHNESS`):
```sql
SELECT MAX({S}) AS last_load FROM {table};      -- or MAX({B}) for batch mode
```
Compared to `max_age_hours` exactly like the watermark freshness today; only the *source column*
differs, declared in rule config `{"source": "effective_from" | "batch_id"}`.

**4. No-overlap / gap** (`scd2_no_overlap`, point-in-time, windowed):
```sql
SELECT COUNT(*) AS violations FROM (
  SELECT {K},
         {S} AS s,
         LAG({E}) OVER (PARTITION BY {K} ORDER BY {S}) AS prev_end
  FROM {table}
) w
WHERE prev_end IS NOT NULL AND s < prev_end;   -- overlap; gap variant: s > prev_end
```
All dialect-aware via the connector, consistent with `evaluator.py`'s existing pushdown model.

### 6.5 New `RuleType`s
Add to `rules.RuleType`: `SCD2_ACTIVE_UNIQUE = "scd2_active_unique"`,
`SCD2_NO_OVERLAP = "scd2_no_overlap"`. `active_count` is modelled as a **metric**, not a new
RuleType, so it gets history/charts for free. Each new type implements the `Rule` protocol
(`to_sql` / `evaluate`) so the runner needs no special-casing.

---

## 7. API surface

Mostly extensions, additive:

- `GET /table-metadata/metadata-presets` *(new)* — the ETL preset library (§3.3) for the first-step
  picker.
- `POST /connections/{id}/metadata-columns:detect` *(new)* — given the selected tables, return the
  best-matching preset + a role→column map + per-table coverage (reuses `watermark` detect/validate).
- `GET /table-metadata/pattern-specs` — now returns the new SCD2 fields
  (`effective_to_column`, `business_key`, `batch_id_column`) and an `scd2` config sub-spec
  (`active_mode`, `high_date_value`, `open_end_is_null`, `current_flag_value`). **Frontend needs
  no change to render them** — `TableLoadConfig.tsx` is data-driven.
- `POST /table-metadata/classify` — richer result (convention-first, end-date SCD2, batch id,
  business key, proposed `high_date_value`, confidence).
- `MonitorCreate` / `MonitorUpdate` — gain `metadata_columns` (preset name + role map) persisted on
  `Monitor` and projected onto each table (§3.4).
- `POST /monitors/{id}/rules:autogenerate` (exists) — extended to emit the **load-aware pack**
  (points 1–4) with `origin=system`, `state` per §6.3 defaults. Idempotent: re-running adds only
  new rules, never edits user-touched ones.
- `GET /monitors/{id}/rules` — rule dicts now include `origin` and keep `state`/`confidence`.
- `PATCH /monitors/{id}/rules/{rid}` — `state` enable/disable (exists); editing flips `origin→user`.
- `POST /monitors/{id}/tables/{ref}/scd2:preview` *(new, optional)* — dry-run the active predicate
  + the four checks against a sample and return counts/violations (mirrors the compare `dry-run`).

All responses keep the `ApiResponse` wrapper; non-fatal issues (e.g. "couldn't sample the end-date
column to infer the sentinel", "3 tables are missing `dw_batch_id`") surface as warnings, never
swallowed (golden rule #3).

---

## 8. UX — a first step, and show the load type clearly

1. **Step 0 of Create Monitor — "Metadata columns".** Before configuring checks, the user picks
   their **ETL tool preset** (or *Auto-detect* / *Custom*). We prefill the role→column map, show a
   **per-table coverage** readout ("`dw_load_ts` found in 18/18 tables; `dw_batch_id` in 16/18"),
   and let them adjust. One declaration configures every table.
2. **Load-type badge** on the table view (`TableObservability.tsx`), legible, e.g.:
   > **SCD Type 2** · active = `src_eff_end_date` ≥ `9999-12-31` · key = `customer_id` · batch =
   > `batch_id` — *detected, 0.9 confidence* · **[Edit]**
   The source chip (`detected` / `user` / `injected`) and confidence come from
   `TableMetadata.source`/`confidence`.
3. **Edit load type** — the per-table config (`TableLoadConfigRow`) gains the new fields
   automatically; an "Active rows defined by…" group makes the predicate choice (flag vs end-date +
   sentinel) explicit.
4. **Active-record count** shown as its own metric card + trend next to Volume, so "total grows but
   active dropped" is immediately visible.
5. **Rule list with provenance + toggles** — each rule row shows a **provenance chip**
   (`Automated` | `Suggested` | `User` | `Injected`), an **enable/disable toggle** (`state`),
   confidence for non-user rules, and the plain-language assertion. Suggested rules are visually
   distinct with an **Enable** affordance. This is the "labels so DQ users know what's automated vs
   user-provided" requirement.

---

## 9. Data model & migration

New idempotent Alembic migration (additive only, safe on existing DBs):

- `connection`: `+ metadata_columns JSON NULL` (the warehouse-wide default — preset name + role
  map — that monitors inherit).
- `monitor`: `+ metadata_columns JSON NULL` (inherited-or-overridden preset name + resolved
  role→column map).
- `table_metadata`: `+ effective_to_column VARCHAR(255) NULL`,
  `+ batch_id_column VARCHAR(255) NULL`, `+ business_key JSON NULL`.
  (`active_mode` / `high_date_value` / `open_end_is_null` / `current_flag_value` and the
  `soft_delete_flag` role ride in the existing `config` JSON — no column needed.)
- `dq_rule`: `+ origin VARCHAR(16) NOT NULL DEFAULT 'user'`.
- `metric_rule`: `+ origin VARCHAR(16) NOT NULL DEFAULT 'user'`.

Backfill: existing rows → `origin='user'` (safe: nothing auto-generated them before). Uses
`IF NOT EXISTS` / guarded `ALTER` per the migration conventions. The preset library and role
catalog are code (one place), not tables.

---

## 10. Testing (end-to-end, no mocks — golden rule #1)

Real local-SQL source (the `local`/`inline` connector used by `tests/test_sample_by_key.py`):

| scenario | setup | expected |
|---|---|---|
| preset prefill | pick `informatica_dw` preset | roles map to `dw_load_ts`/`dw_batch_id`/… ; coverage reported |
| preset (dbt) | `dbt_snapshot` on a snapshot table | `scd2`, `active_mode=end_date`, `open_end_is_null=true` via `dbt_valid_to` |
| coverage warning | a table missing `dw_batch_id` | warning lists the table; others still configured |
| detection (end-date SCD2) | profile `src_eff_*` table | `scd2`, roles populated, `high_date≈9999-12-31`, key=`customer_id` |
| detection name-tolerance | columns prefixed `src_eff_*` | still detected (substring match) |
| active predicate | mix of open (`9999-12-31`) + closed rows | active count = #open rows |
| soft-delete exclusion | some rows `_fivetran_deleted=true` | excluded from active + volume |
| active-count baseline | active pop drops run-over-run | metric `fail`/`warn`; total count unchanged |
| one-active-per-key (bug) | a key with two open rows | `scd2_active_unique` fails; offending key in evidence |
| batch freshness | no new `batch_id` past SLA | freshness `fail` |
| no-overlap (bug) | overlapping `[start,end)` for a key | `scd2_no_overlap` fails |
| provenance/enable | autogenerate pack, disable one, edit one | states + `origin` labels correct; re-autogen doesn't touch user-edited rule |

Frontend: `tsc` clean; new fields render from the spec catalog with no component edits.

---

## 11. Phasing

1. **P1 — Metadata columns + config + detection + show.** Step-0 preset/auto-detect, new roles +
   active predicate + convention-first classifier + a badge that shows the resolved semantics.
   *No behavioural detection yet — but tables are finally described correctly from one declaration.*
2. **P2 — Load-aware rule pack + provenance.** `origin` field, autogenerate the pack, the rule-list
   UI with labels + toggles, checks #1 (active count) and #2 (active-unique).
3. **P3 — Freshness-from-batch (#3) and no-overlap (#4)**, plus the `scd2:preview` dry-run.

Each phase ships with its functional tests and is independently useful.

---

## 12. Open questions

1. ~~**Where does the convention live?**~~ **Resolved:** a **connection/org-level default** set
   once per warehouse, inherited by every monitor (overridable per monitor, then per table). The
   default is **advisory** — applied per-table only where the columns exist, with graceful
   auto-detect/safe-default fallback for tables that don't conform (§3.4–3.5).
2. ~~**Preset library scope / user-extensible?**~~ **Resolved:** conventions are a first-class,
   org-scoped **library** (§3.4) — shipped presets are `sys:` conventions, users save their own.
   Connections attach one or more; a connection with **two ETL tools** (e.g. IDMC-CDC + S3-Loader)
   attaches both, and each table resolves to the **highest-coverage** convention automatically.
3. **Active definition precedence** when *both* a flag and an end-date exist — recommend the
   **flag** wins (explicit), end-date is the fallback; configurable via `active_mode`. Confirm.
4. **High-date sentinel** — default `9999-12-31`? Some shops use `9999-12-31 23:59:59`, `2999-…`,
   or NULL-for-open. We infer + let the user override; is one value per table enough or do we need
   a *list* of accepted sentinels?
5. **Composite business keys** — supported (`business_key` is a list); confirm the UI lets a user
   pick multiple columns.
6. **Active-count home** — first-class metric (chart/history, recommended) vs. a `DQRule`.
7. **Cross-engine provenance** — `origin` added to both `dq_rule` and `metric_rule`; acceptable, or
   do we want a unified "assertion" table later (larger refactor, out of scope here)?

---

## 13. Multi-layer deployment & sample-based rule packs (implemented)

Validated against a real deployment shape: an ingestion framework stamps metadata columns **only in
the source layer**; **curate and publish layers have none**. One monitor per schema (≈17 monitors:
5 source + 2 curate + 10 publish; up to ~30 tables each).

**Graceful degradation (already in §3.5).** The ETL convention is advisory and per-table: source
tables resolve their roles and get watermark/SCD2 behaviour; curate/publish tables (no audit
columns) fall through to auto-detect → safe defaults. No crash, no blocked monitor. A monitor that
spans a source table (with a load column) and a curate table (without) samples the former by its
watermark and the latter by first-N — the sampler orders by the watermark only when the table
actually has it.

**Auto-create suggested rules from a sample (no full scan).**
`POST /monitors/{id}/rules:suggest-pack` profiles a **sample of each selected table's last batch**
and generates a suggested pack: column DQ rules from the profile (NOT_NULL / ROW_COUNT / NULL_RATE
/ RANGE / UNIQUE / REGEX — metadata-independent, so curate/publish are fully covered) plus the SCD2
load-aware rules where applicable. Properties:
- **Per-table selection** (`tables`), so the user checks/unchecks which tables get rules.
- **Sample-based, configurable** (`sample_size`, default 1000; `level` 1–3) — never a full scan.
- Each rule is dry-run against the same sample (pass/fail preview) and created as
  **`state="suggested"`, `origin="system"`** → the user **accepts (enable) or removes** it.
- **Idempotent** by `(table_ref, type, name)`; `persist=false` returns a preview only.
- Wired into Create Monitor as an "Auto-create DQ rules" card (per-table toggles + sample size).

**Warehouse-native freshness (resolves §12 Q-freshness → LAST_ALTERED).** For a table with no load
/ watermark column, a freshness rule with `config.source="warehouse"` reads the table's last-altered
time from the warehouse catalog (Snowflake `INFORMATION_SCHEMA.TABLES.LAST_ALTERED`; connectors
expose `last_altered(table)`) instead of `MAX(column)` — so curate/publish tables still get a
freshness signal. Unavailable last-altered surfaces as a plain reason, not a false green.
