# 10 — Production Data Observability (Data Model, Scenarios & Roadmap)

> Framing: *"This is not a POC. Think like a CEO and CTO. A user has data loading from
> 5 sources, many ETL jobs, and multiple layers (bronze/silver/gold). Come up with all
> the real scenarios and keep handling them. The UX has to be genuinely helpful."*

This doc defines the target data model and execution model for a **production-grade**
data observability platform, the real-world scenarios it must handle, how **custom data
quality rules and their metrics** fit, the UX/information architecture, a **testing
scenario matrix**, and a **phased roadmap**. It extends `04-observability.md` and
`07-data-model.md` and supersedes their scope where noted.

---

## 1. What "production" means here (principles)

1. **Observe the pipeline, not just tables.** A broken dashboard is almost always an
   upstream job/source problem. We model **sources → jobs/runs → datasets → columns →
   rules → incidents** and connect them with **lineage** so we can do impact analysis
   and root-cause, not just per-table red/green.
2. **Every signal is a time series.** Freshness, volume, schema, distribution, custom
   rules — all land as metrics with history, baselines and thresholds. Charts and
   anomaly detection fall out of one metric store.
3. **Thresholds are both declared and learned.** Start with static thresholds + SLAs;
   layer rolling baselines and ML/seasonal anomaly detection to cut false positives.
4. **Failures are incidents with a lifecycle**, owners, severity, dedup and routing —
   not just a failed check. Anomaly *budgets* decide what pages a human.
5. **Never silently drop.** Bad records are sampled/quarantined and surfaced (golden
   rule #3). Partial success is first-class (`ApiResponse.warnings`).
6. **Cost-aware.** Pushdown SQL, watermark-incremental scans, sampling, and per-run
   cost accounting are design constraints, not afterthoughts.
7. **Multi-tenant & governed by default** (see `02-multi-tenancy.md`): every entity
   carries `org_id`/`group_id`; PII-aware sampling; full audit.

---

## 2. The pillars (industry 5 + enterprise extensions)

Monte Carlo's canonical five pillars are **Freshness, Volume, Schema, Distribution
(quality), Lineage**. For 2026-scale enterprise we add **Pipeline** and **Cost**, and
keep a hook for **Semantic integrity** (AI/contract drift).

| Pillar | Question it answers | Primary metrics |
|--------|---------------------|-----------------|
| **Freshness** | Did data arrive on time? | load lag, time-since-last-load, run cadence vs SLA |
| **Volume** | Did the expected amount arrive? | row count, delta rows, % change vs baseline |
| **Schema** | Did the shape change unexpectedly? | column add/drop/retype, nullability/PK drift |
| **Quality/Distribution** | Are values sane? | null rate, uniqueness, ranges, cardinality, pattern match, **custom rules** |
| **Lineage** | Where from, what's impacted? | upstream/downstream edges, blast radius |
| **Pipeline** (ext.) | Are the jobs healthy? | job run status, duration, retries, failure rate |
| **Cost** (ext.) | What is monitoring/compute costing? | bytes/credits scanned per run, per monitor |

**Medallion nuance** (bronze/silver/gold each need different monitoring — per research):

- **Bronze** (raw landing): ingestion completeness, **file/partition arrival SLA**, basic type/null enforcement, source-vs-landing row reconciliation.
- **Silver** (conformed): schema conformance, null rates, **referential integrity**, dedupe/uniqueness, **quarantine** of failing rows (flagged, not dropped).
- **Gold** (serving): metric/KPI consistency, **aggregate-ties-to-detail reconciliation (within tolerance, e.g. 0.1%)**, freshness SLAs feeding dashboards/ML.

---

## 3. Real-world scenarios we must handle

A concrete customer: **5 sources** (Salesforce, Postgres CDC, S3 event files, Kafka,
a vendor API) → **Bronze** raw tables → **Silver** conformed → **Gold** marts, wired by
**dozens of ETL jobs** (Airflow/dbt). Scenarios the platform must cover:

| # | Scenario | What the platform must do |
|---|----------|---------------------------|
| S1 | A source stops delivering (API outage) | Freshness breach on bronze → incident → lineage shows impacted silver/gold → suppress duplicate downstream alerts |
| S2 | Partial/late/backfill loads | Watermark-incremental; late-arriving data reconciled; backfills don't false-alarm volume |
| S3 | Schema change upstream (column dropped/retyped) | Schema drift detected at bronze; impact analysis to downstream consumers; breaking vs non-breaking classification |
| S4 | Volume spike/drop (2× or −60%) | Anomaly vs seasonal baseline (weekday/weekend, month-end); not a flat threshold |
| S5 | Distribution shift (null rate climbs, new category, out-of-range) | Column-level distribution metrics + custom rules; drift alerting |
| S6 | Duplicate / PK violation after a bad merge | Uniqueness/referential-integrity rules at silver |
| S7 | Gold KPI doesn't tie to detail | Cross-layer **reconciliation** rule (sum(gold) ≈ sum(silver) within tolerance) |
| S8 | An ETL job fails or runs long | Pipeline pillar: job-run status/duration; correlate run → affected datasets |
| S9 | Business-specific invariant ("revenue ≥ 0", "status in set", "order_date ≤ ship_date") | **Custom DQ rules** (templated + custom SQL), with their own metrics & history |
| S10 | Same table watched by multiple teams/monitors | Multi-tenant isolation; per-monitor rule bindings; no metric bleed |
| S11 | Huge tables / warehouse cost | Pushdown aggregation, sampling, partition-pruned scans, per-run cost caps |
| S12 | Noisy alerts / alert fatigue | Dynamic thresholds, anomaly budgets, incident dedup, mute windows, ownership routing |
| S13 | PII columns | PII tagging; hashed/partial samples; redaction in UI; access control |
| S14 | Audit / compliance | Immutable audit of rule edits, incident actions, who-acked-what |

Section 10 (testing) maps each scenario to concrete test cases.

---

## 4. Target data model

Builds on existing tables (`connection`, `monitor`, `dq_rule`, `monitor_run`,
`rule_result`, `metric`, `incident`, `catalog_table`, `audit_log`). **New/expanded**
entities in **bold**. All tenant tables carry `org_id`/`group_id`.

### 4.1 Catalog & topology
- **`source`** — a logical origin (Salesforce, Postgres CDC, S3…). `kind`, `layer_hint`, owner. A `connection` is the *access*; a `source` is the *thing*.
- `connection` *(exists)* — access/auth to a source/warehouse.
- **`dataset`** (generalises `catalog_table`) — a table/view/file-set. Fields: `connection_id`, `database/schema/name`, **`layer`** (`bronze|silver|gold|other`), `object_type`, `owner`, `tags`, `pii` flags, `row_count`, `column_meta`, `is_monitored`.
- **`dataset_column`** — first-class columns (name, type, nullable, **`pii_class`**, description) so column-level rules/metrics/lineage attach cleanly.
- **`lineage_edge`** — `upstream_dataset_id → downstream_dataset_id` (+ optional column-level), `relation` (`derive|copy|aggregate`), `produced_by_job_id`. Enables impact analysis (S1,S3).

### 4.2 Pipelines (Pipeline pillar)
- **`pipeline`** — a logical ETL job (dbt model, Airflow task). `name`, `source_ref`, `schedule`, `owner`.
- **`pipeline_run`** — one execution: `status`, `started/finished`, `rows_written`, `duration`, `retries`, `error`, writes-to `dataset_id`(s). Correlate job health ↔ dataset freshness (S8).

### 4.3 Monitors, rules, bindings
- `monitor` *(exists, + watermark)* — a scope of datasets with schedule, watermark, SLA refs.
- **`rule`** *(generalises `dq_rule`)* — a DQ check **definition**. Fields: `type` (built-in enum **or** `custom_sql`/`metric`), `columns`, `config` (JSON), `severity`, `scope` (`incremental|full`), `state` (`suggested|active|muted|disabled`), `confidence`, `owner`, `dimension` (which pillar), **`expression`** (for custom SQL), **`threshold`** config (static/anomaly), `version`, soft-delete. (Today's `dq_rule` already has most of this.)
- **`rule_binding`** — binds a rule to specific datasets/columns (so one "revenue ≥ 0" rule can target many tables). For simple cases a rule carries its own `table_ref`.
- **`data_contract`** *(later)* — a declarative spec (schema + freshness + rules) attached to a dataset; violations open incidents (S3,S9).

### 4.4 Runs, results, metrics (the time-series spine)
- `monitor_run` *(exists)* — one evaluation pass over a dataset (watermark predicate, rows analysed, ruleset snapshot, cost).
- `rule_result` *(exists)* — per-rule outcome in a run: `passed`, `metric_value`, `threshold`, `observed`, `bad_sample_ref`. **This is how custom-rule metrics are captured.**
- `metric` *(exists)* — the unified time series. Naming convention (already used for pillars): `freshness.lag_seconds`, `volume.row_count`, `schema.column_count/drift_count`, `quality.score`, column metrics `quality.null_rate`/`quality.distinct_ratio` (tags.column), and **`rule.<rule_id>`** for every custom/built-in rule (tags: `monitor_id`, `rule_id`, `rule_name`, `severity`, `dimension`, `table_ref`, optional `column`). One store → cards, charts, anomaly detection, rule metrics.
- **`metric_baseline`** — learned/declared expectation per `(metric, segment)`: `method` (`static|rolling|seasonal|ml`), params, `lower/upper`, seasonality. Powers dynamic thresholds (S4,S5,S12).
- **`segment`** *(optional)* — a dimension slice (e.g. `region=EU`) so metrics/rules run per-segment.

### 4.5 Incidents, SLAs, alerting
- `incident` *(exists, expand)* — lifecycle `open→acknowledged→investigating→resolved`, `severity`, `dimension`, `root_cause_ref` (lineage), **`dedup_key`**, assignee, timeline. Opened by rule failures / anomalies / SLA breaches.
- **`sla`** / **`slo`** — freshness/volume/quality targets per dataset (e.g. "gold.orders fresh < 1h, 99.5%"), with **anomaly budget**. SLA breach → incident (S1,S7).
- **`alert_channel`** + **`alert_route`** — where/whom to notify (email/Slack/Teams/webhook), routing by owner/severity/dimension, **mute windows**, dedup (S12). Reuses the platform's existing notification service pattern.

### 4.6 How scenarios map to the model (traceability)
S1 freshness+lineage+sla+incident · S2 monitor.watermark+monitor_run.partition_predicate ·
S3 schema metrics+lineage_edge+incident · S4 metric_baseline(seasonal) · S5 column metrics+rules+baseline ·
S6 uniqueness/RI rules+rule_result · S7 reconciliation rule across datasets+sla · S8 pipeline_run↔dataset ·
**S9 rule(custom_sql/metric)+rule_result+metric `rule.*`** · S10 tenant cols+rule_binding · S11 monitor_run cost + sampling ·
S12 metric_baseline+anomaly budget+alert_route+incident.dedup_key · S13 dataset_column.pii_class+sample redaction · S14 audit_log.

---

## 5. Custom data quality rules + their metrics  *(the explicit new requirement)*

**Users must be able to add their own DQ rules and see each rule's metric over time.**

### 5.1 Rule kinds
1. **Templated/built-in** (already in `RuleType`): `not_null`, `null_rate`, `unique`,
   `row_count`, `range`, `in_set`, `regex`, `freshness`. Config-driven (threshold,
   min/max, set, pattern). Low-risk, pushdown-friendly.
2. **Custom SQL rule** (`custom_sql`): user supplies a boolean/aggregate expression or a
   full query that returns a `metric_value` (and optionally `failing_rows`). Example:
   `SELECT COUNT(*) FROM {{table}} WHERE revenue < 0` → metric = violation count; pass if
   `metric_value == 0`. This is the Soda "user-defined metric" / dbt "singular test"
   pattern. Executed via **pushdown** on the warehouse.
3. **Metric rule** (`metric`): track an arbitrary numeric expression as a first-class
   metric with a threshold/anomaly baseline (e.g. `avg(order_value)`), charted over time
   even when it never "fails".
4. **Reconciliation rule** (later): compares two datasets/queries within a tolerance (S7).

### 5.2 Config shape (stored in `rule.config`)
```jsonc
// custom_sql example
{
  "expression": "SELECT COUNT(*) AS v FROM {{table}} WHERE amount < 0",
  "pass_when": "value == 0",         // or "value <= threshold"
  "threshold": 0,
  "dimension": "quality",            // which pillar it rolls up to
  "severity": "critical",
  "sample_sql": "SELECT * FROM {{table}} WHERE amount < 0 LIMIT 100"
}
```
Rules keep a `state` (`suggested|active|muted|disabled`) and `version` (edits bump it;
in-flight runs use the snapshotted version — already implemented).

### 5.3 Capture → metric → display (how metrics flow)
1. **Evaluate** (executor / pushdown, or an external agent): compute `metric_value`,
   `passed`, `threshold`, `observed`, optional `bad_sample_ref`.
2. **Record**: write a `rule_result` (linked to a `monitor_run`) **and** a `metric`
   row `rule.<rule_id>` (value = `metric_value`, tags carry rule/severity/dimension).
   → This mirrors the existing snapshot/profile capture path, so it's testable locally
   without a warehouse (push results in; read metrics back out — no mocks).
3. **Display**: 
   - Per **table page**: a **Data Quality Rules** panel — each rule shows latest
     pass/fail, metric value vs threshold, severity, and a **sparkline of its metric
     history**; the rule rolls into its pillar's status.
   - Per **rule**: a detail view with the full metric time series + recent results +
     failing-row sample.
   - Rule failures feed the **Quality** pillar status and can open **incidents**.

### 5.4 API surface (additive to today's `/monitors/{id}/rules` CRUD)
- `POST /monitors/{id}/rules` *(exists)* — create a rule (now also `custom_sql`/`metric`).
- `POST /monitors/{id}/rules/{rule_id}:record` — record one evaluation result (capture path).
- `GET  /monitors/{id}/rules/{rule_id}/history` — the rule's metric + pass/fail time series.
- `GET  /monitors/{id}/tables/{table_ref}/rules` — rules bound to a table + latest result (powers the table panel).
- *(later)* `POST /monitors/{id}/rules/{rule_id}:evaluate` — run it now via pushdown.

> **Phase C1 (built this iteration)** implements create (incl. custom SQL/metric),
> `:record`, `/history`, and `/tables/{t}/rules`, plus the UI panel — so you can add a
> custom rule and watch its metric immediately. The **pushdown evaluator** (actually
> running custom SQL on Snowflake) is Phase C2.

---

## 6. Metrics & anomaly detection

- **Static** thresholds + **SLAs** first (deterministic, explainable).
- **Rolling baselines** (mean/stddev, MAD) per metric for drift (S4,S5).
- **Seasonal** decomposition (weekday/weekend, month-end) to avoid false alarms.
- **ML / SPRT** for early detection with **anomaly budgets** to bound paging (per research).
- Stored in `metric_baseline`; every pillar card and rule can show "expected band" vs actual.

## 7. Execution & cost

- **Pushdown-first**: one batched aggregate query per monitor run computes pillar +
  rule metrics; only results return to the app (existing connector contract).
- **Watermark-incremental** (already modelled): scan only rows past the high-water-mark.
- **Scheduler**: cron per monitor → enqueue runs (job queue abstraction exists).
- **Sampling + partition pruning + per-run cost caps** for big tables (S11); record
  bytes/credits on `monitor_run` (Cost pillar).

## 8. UX / information architecture (must be genuinely helpful)

Navigation mirrors the mental model **Overview → Pipelines/Sources → Datasets → Table →
Column/Rule → Incident**:

1. **Observability Overview** — health heatmap across layers (bronze/silver/gold), open
   incidents, freshness/volume at a glance, "what's broken & what it impacts".
2. **Dataset/Table page** *(built)* — four pillar cards + **charts by default**,
   lineage, **Data Quality Rules panel** (Phase C1), column drill-down, "Profile now".
3. **Rule view** — metric history, results, failing-row sample, mute/own.
4. **Incident view** — timeline, lineage blast-radius, suspected root cause, ack/resolve.
5. **Create/Edit Monitor** *(built)* — watermark auto-detect/validate, rule suggestions,
   SLA setup.

Helpful-by-default patterns: dynamic-threshold bands drawn on charts; "why is this red?"
always one click to the failing rule/sample; impact ("this affects 3 gold tables")
surfaced on every incident; sensible autogen rules proposed on monitor creation.

## 9. Gap analysis vs current scaffold

| Area | Today | Gap to production |
|------|-------|-------------------|
| Pillars (F/V/S/Q) + history | ✅ built | add baselines/anomaly bands; pipeline+cost pillars |
| Watermark detect/validate | ✅ built | wire into scheduler/incremental runs |
| Rules CRUD + autogen | ✅ model + API | **custom-rule metrics + UI (C1)**; pushdown evaluator (C2) |
| Capture (snapshot/profile) | ✅ built | real pushdown profiler on Snowflake |
| Catalog/columns/layers | partial (`catalog_table`) | `dataset`/`dataset_column`/`layer`, lineage edges |
| Pipelines/job runs | ❌ | `pipeline`/`pipeline_run` + correlation |
| Incidents/SLA/alerting | model stub | lifecycle, SLA/anomaly budget, routing/dedup |
| Anomaly detection | ❌ | baselines + seasonal/ML |
| Scheduler | abstraction only | cron → runs, retries, cost caps |

## 10. Testing strategy — scenario matrix (no mocks; golden rule #1)

Every scenario gets deterministic coverage. Three layers: **pure unit** (logic), **API
E2E** (real in-memory stack via the `inline` connector), **functional** (real HTTP). We
add a **synthetic data/scenario generator** that drives the `inline` connector + capture
endpoints to reproduce each scenario without a warehouse.

| Scenario | Test |
|----------|------|
| S1 freshness breach + impact | push stale freshness → pillar critical → (C-incident) opens; lineage lists downstream |
| S2 incremental/backfill | watermark predicate limits scan; backfill snapshot doesn't trip volume anomaly |
| S3 schema drift | snapshot with changed column set → `schema.drift_count>0` → schema warning |
| S4 volume anomaly | series around seasonal baseline → drop beyond band flagged |
| S5 distribution drift | rising `quality.null_rate` → column drill-down + rule fires |
| S6 uniqueness/RI | `unique` rule result failing → quality critical |
| S7 reconciliation | recon rule value beyond tolerance → fail |
| S8 pipeline health | pipeline_run failed → correlated dataset flagged |
| **S9 custom rule metric** | create custom_sql rule → `:record` values → `/history` returns series; panel shows pass/fail + chart |
| S10 isolation | second group cannot see group A's rules/metrics (existing isolation tests extended) |
| S11 cost | monitor_run records bytes/credits; cap enforced |
| S12 noise | dedup_key collapses duplicate incidents; mute window suppresses |
| S13 PII | sample redaction for pii columns |
| S14 audit | rule edit / incident ack appear in audit_log |

Existing: 50 pytest + a functional script already cover watermark + the four pillars end
to end. New scenarios extend both.

## 11. Phased roadmap

- **C1 — Custom DQ rules + metrics (✅ built):** custom_sql/metric rule types,
  `:record`, `/history`, `/tables/{t}/rules`, table-page Rules panel with sparklines.
- **C2 — Pushdown rule evaluator + scheduler (✅ built):** `evaluator.py` compiles
  built-in + custom SQL to one aggregate query and runs it via the connector (Snowflake
  in prod, in-memory SQLite `local` connector in dev/tests); `MonitorRunner` profiles
  every table (volume/schema/freshness + per-column null rates → quality) and evaluates
  active rules; exposed as `POST /monitors/{id}:run` and `…/rules/{id}:evaluate`; an
  opt-in background `Scheduler` runs active monitors on an interval. *Follow-up:* precise
  per-monitor cron scheduling + multi-tenant fan-out.
- **C3 — Catalog & lineage:** `dataset`/`dataset_column`/`layer`, lineage edges, impact
  analysis; Observability Overview (medallion heatmap).
- **C4 — Pipelines:** `pipeline`/`pipeline_run`, job↔dataset correlation (Pipeline pillar).
- **C5 — Incidents, SLAs, alerting:** lifecycle, SLA/anomaly budgets, routing/dedup/mute.
- **C6 — Anomaly detection:** rolling/seasonal baselines, bands on charts, ML/SPRT.
- **C7 — Data contracts, cost governance, PII/governance hardening.**

---

### Sources (research grounding)
- Monte Carlo — [What Is Data Observability? 5 Key Pillars](https://montecarlo.ai/blog-what-is-data-observability)
- Soda — [5 Pillars of Data Observability](https://soda.io/blog/data-observability-five-pillars) · [Soda Core: checks, contracts, CI/CD](https://www.datasops.com/blog/soda-data-quality)
- DQLabs — [7 Pillars of Data Observability (2026)](https://www.dqlabs.ai/blog/the-5-pillars-of-data-observability-and-the-2-more-that-define-it-at-enterprise-scale/)
- Atlan — [Data Observability for AI Pipelines: the sixth pillar](https://atlan.com/know/ai-agent/data-for-ai/data-observability-for-ai-pipelines/)
- [Data Quality Tooling Compared: Great Expectations, Soda, dbt Tests](https://datalakehousehub.com/blog/data-quality-tooling-compared/) · [datacontract.com — Quality Rules](https://docs.datacontract.com/quality-rules)
- Nimble — [Data Quality & Monitoring with the Medallion Architecture](https://www.nimbleway.com/blog/data-monitoring-with-medallion-architecture)
- Acceldata — [Automated Anomaly Detection](https://www.acceldata.io/blog/data-observability-with-automated-anomaly-detection-explained) · ResearchGate — [SLA Design, Anomaly Budgets & SPRT for payments pipelines](https://www.researchgate.net/publication/396759593_Data_Observability_for_High-Throughput_Payments_Pipelines_SLA_Design_Anomaly_Budgets_and_Sequential_Probability_Ratio_Tests_for_Early_Incident_Detection)
