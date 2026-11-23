# DV Platform — Roadmap & Status

> Single reference for where the platform is and where it's going, across **both**
> products: **Data Observability** (monitor data health) and **Data Validation / Testing**
> (compare datasets across systems). Keep this current — it's the map we steer by.
>
> Deep designs live in [`docs/design/`](./docs/design/). This doc is the status + plan
> layer on top of them.

**Legend:** ✅ built & tested · 🟡 partial (core done, gaps noted) · ⬜ planned · 🔒 needs live warehouse

_Last updated: 2026-10-07 · Gates: 72 pytest, 25/25 functional checks, 35 API routes, frontend builds._

---

## 0.5 Competitive strategy — how we win (best-in-class)

Grounded in a market scan (Monte Carlo, Soda, Anomalo, Bigeye, Metaplane→Datadog,
Datafold, GX, Acceldata, Databand, Elementary, Sifflet, Lightup) + the proven DataQ
engines (datadiff, DMS). Two market shifts matter: **Datadog absorbed Metaplane** (an
incumbent folding data-obs into unified ops) and **GX Cloud was discontinued** (a gap for
self-serve). The category is sprinting toward **AI/agent-native**.

**Our wedge (what no incumbent owns): one engine for the full loop — _prevent_ (contracts
+ CI diff) → _detect_ (monitors + anomaly) → _diagnose_ (lineage RCA + incidents) →
_verify_ (data diff the fix).** The market splits these across three tools/budgets:
Monte Carlo/Bigeye _detect_, Soda/GX/dbt _assert_, Datafold _diffs_. **We already build
observability AND validation in one platform** — that's the story to press.

**The #1 reason teams churn their tool = alert fatigue.** So our brand guarantee:
**every alert ships with evidence (metric + expected band + sample + suspected cause) or
it doesn't ship**, with dedup + auto-resolve. (Shipped this in the incident engine.)

**Table-stakes we must have (close these to be credible):**
five canonical monitors ✅, warehouse connectors (Snowflake real; Oracle/PG next),
ML/auto-thresholds ✅(robust baseline), Slack/Teams/PagerDuty alerting ⬜, table +
**column-level lineage** ⬜, one-click auto-monitoring on connect ⬜, incident feed ✅,
code+no-code rule authoring 🟡, dbt test ingestion ⬜, RBAC/SSO/audit ✅.

**Differentiators to win on (bold bets):**
1. **Agentic triage→fix→verify loop**: detect → RCA across lineage + git/query logs →
   **draft the fix as a PR** → **data-diff proves the fix before merge** (human-approved).
   Nobody closes this loop end to end. *(We uniquely can — we own the diff engine.)*
2. **Unified prevent+detect+diagnose+verify** in one tool (the wedge above).
3. **NL monitor authoring** — "alert if daily revenue drops >15% vs last 4 Tuesdays" →
   generated SodaCL/SQL check, reviewable as code.
4. **Data + AI/LLM observability in one pane** (feature/RAG freshness, LLM output quality
   on the same lineage/incident model) — where Datadog/Monte Carlo are racing.
5. **Trust-first anti-noise**: per-monitor precision/recall the user tunes with feedback;
   evidence on every alert. *(Foundation shipped.)*

**Beat-the-field notes:** transparent usage-based pricing + real free tier (undercut the
$45K–$100K floors); work on **thin history** (degrade gracefully — the robust-baseline
anomaly detector already does this); don't out-platform Datadog on infra — be
**data-native + AI-agent-native**. Full analysis: see the research in the session log and
`docs/design/10 §2/§6`.

---

## 0.6 Source independence — one contract, every capability on every source

**Principle:** data sources are decoupled from the application. Every capability —
**count compare, schema compare, data validation (data compare), and data quality
(rule evaluation + profiling)** — is implemented **once** against the `Connector` protocol
(`test / get_schema / count / execute / latest_watermark / sql_ref / dialect`). There is
**no per-source branching** in the engine; a new source just implements the protocol and
immediately gets all four capabilities. Each source **owns its dialect** (regex/limit
quirks), so the engine stays source-agnostic. Sources that don't speak SQL natively
(files/S3) back themselves with an embedded SQLite engine, so `execute/count/get_schema`
still hold.

| Capability ↓ / Source → | Snowflake | Oracle | File / S3 | Local/Inline | Postgres… |
|---|---|---|---|---|---|
| Count compare | ✅* | ✅* | ✅ | ✅ | ⬜ (impl adapter) |
| Schema compare | ✅* | ✅* | ✅ | ✅ | ⬜ |
| Data compare (4-bucket) | ✅* | ✅* | ✅ | ✅ | ⬜ |
| Data quality (rules + profiling) | ✅* | ✅* | ✅ | ✅ | ⬜ |

`*` connector is real; needs live creds/host to exercise. File/S3 and Local are proven
end-to-end locally (`tests/test_file_connector.py` runs all four capabilities on a CSV).
**Adding Postgres/BigQuery/etc. = implement one `Connector`; zero engine changes.**

---

## 0. The two products (and why they share a spine)

| Product | Question | Status |
|---------|----------|--------|
| **Data Observability** | "Is my data healthy — fresh, complete, correct, unchanged?" | 🟡 core built (C1–C2); roadmap C3–C7 |
| **Data Validation / Testing** | "Do these two datasets match — across envs, DBs, before/after a migration or ETL?" | 🟡 engine + API built; live execution + UI are V1–V6 |

They share one spine: **connectors** (pushdown SQL / Spark), **tenancy & RBAC**, the
**metadata store**, the **ResultStore** (where real-data samples/diffs land — data
residency), the **API envelope**, and the **React UI shell**. Investing in the spine
advances both products.

---

## 1. Current state snapshot

### 1.1 Platform foundation
| Capability | Status | Where |
|-----------|--------|-------|
| Runtime modes (local / server / prod) | ✅ | `dvp.config`, `docs/design/01` |
| Multi-tenancy + RBAC (org/group, roles, permissions) | ✅ | `dvp.core.tenancy`, `docs/design/02` |
| Metadata store (SQLite local / Postgres prod) + **concurrency-safe sessions** | ✅ | `dvp.db.session` |
| Flyway-style Alembic migrations (idempotent) | ✅ | `migrations/` (0001, 0002) |
| Connector protocol (`test/get_schema/count/execute/latest_watermark/sql_ref`) | ✅ | `dvp.connectors.base` |
| Snowflake connector | 🟡 real impl; needs live creds to exercise | `dvp.connectors.snowflake` |
| **Oracle connector** (python-oracledb thin; OWNER.TABLE, ROWNUM sampling) | ✅ | `dvp.connectors.oracle` |
| **File/S3 connector** (CSV/JSON/NDJSON/Parquet from path or `s3://`) | ✅ | `dvp.connectors.file_source` |
| **InlineConnector** (schema/counts, no deps) | ✅ | `dvp.connectors.inline` |
| **LocalSqlConnector** (real SQL over in-memory SQLite) | ✅ | `dvp.connectors.local_sql` |
| Postgres / SQL Server / BigQuery | ⬜ | one adapter each |
| Secret store (memory/keyring/vault) | 🟡 memory wired; others stubbed | `dvp.abstractions.secret_store` |
| ResultStore (local FS / object / Snowflake-native) | 🟡 local FS wired; object/SF-native planned | `dvp.abstractions.result_store` |
| API (FastAPI, `ApiResponse`, problem+json) | ✅ | `dvp.api` |
| **React + Vite UI** (Tailwind, charts) | ✅ observability pages; validation UI ⬜ | `frontend/` |
| MCP server | 🟡 designed/stub | `dvp.mcp`, `docs/design/06` |

### 1.2 Data Observability
| Capability | Status | Where |
|-----------|--------|-------|
| Monitors + DQ rules CRUD, 3-level autogen | ✅ | `dvp.observability.service`, `autogen.py` |
| **Watermark** auto-detect + validate + monitor-level config | ✅ C-feature | `observability/watermark.py`, `services/watermark.py` |
| **Four pillars** (freshness/volume/schema/quality) + history + column drill-down | ✅ | `observability/pillars.py`, `observability_service.py` |
| **Custom DQ rules + metrics** (custom_sql/metric, record, history, panel) — **C1** | ✅ | `RuleEvaluationService`, `RulesPanel.tsx` |
| **Pushdown evaluator + runner + scheduler** — **C2** | ✅ | `observability/evaluator.py`, `runner.py`, `scheduler.py` |
| Catalog / lineage / medallion Overview — **C3** | ⬜ | planned |
| Pipelines / job-run correlation — **C4** | ⬜ | planned |
| Incidents / SLAs / alerting — **C5** | ⬜ | model stub |
| Anomaly detection (baselines, seasonal, ML) — **C6** | ⬜ | planned |
| Data contracts / cost governance / PII — **C7** | ⬜ | planned |

### 1.3 Data Validation / Testing (the compare engine)
| Capability | Status | Where |
|-----------|--------|-------|
| Compare job model + lifecycle (`queued→running→succeeded/partial/failed`) | ✅ | `CompareJob`/`CompareResult`, `testing_engine/service.py` |
| API: create job, get job, paged `differences` | ✅ | `api/routers/compares.py` |
| **Type normalization** (canonical types, value equality, tolerance, null/space rules) | ✅ | `testing_engine/typemap.py` |
| **Strategy selector** (server-EXCEPT / pushdown / Spark-adaptive / Spark hash-greedy) | ✅ pure logic | `testing_engine/strategy.py` |
| **Key detection** (name-priority + keyable-type filter) | 🟡 pure core; live sample+source-validate ⬜ | `testing_engine/keydetect.py` |
| **Cell-level row compare** (matched/mismatch/src-only/dst-only + per-column counts) | ✅ pure/in-memory | `testing_engine/rowcompare.py` |
| Result persistence (summary→DB, detail→ResultStore) + drill-down | ✅ | `testing_engine/service.py` |
| **Count compare** over live connectors | ⬜ 🔒 | design `05 §2` |
| **Schema compare** over live connectors | ⬜ 🔒 | design `05 §3` |
| **Data compare execution** (pushdown EXCEPT/JOIN; Spark cross-DB) | ⬜ 🔒 | design `05 §7` |
| Batch of table pairs + continue-on-error `partial` | ⬜ | design `05 §1,9` |
| **Compare UI** (diff grid, cell-level drill-down) | ⬜ | — |
| Snowflake-native ResultStore (data residency) | ⬜ | design `05 §8.1` |

> **Reading:** Data Validation has a **correct, tested engine and job/result plumbing**;
> what's missing is **executing it against live Snowflake/Oracle** (pushdown + Spark) and
> a **UI**. That's the V-roadmap below.

---

## 2. Data Observability roadmap (C-phases)

- **C1 — Custom DQ rules + metrics** ✅ *(done)* — custom_sql/metric rule types; `:record`, `/history`, `/tables/{t}/rules`; table-page Rules panel with sparklines.
- **C2 — Pushdown evaluator + scheduler** ✅ *(done)* — `evaluator.py` compiles built-in + custom SQL → one aggregate query via the connector; `MonitorRunner` profiles tables (volume/schema/freshness + per-column null-rates → quality) and evaluates active rules; `POST /monitors/{id}:run` + `…/rules/{id}:evaluate`; opt-in `Scheduler`. *Follow-up:* per-monitor cron + multi-tenant fan-out.
- **C3 — Catalog, lineage & Overview** ⬜ — `dataset`/`dataset_column`/`layer` (bronze/silver/gold), lineage edges, impact analysis, **Observability Overview** (medallion health heatmap). *High UX value; matches the 5-sources/multi-layer vision.*
- **C4 — Pipelines** ⬜ — `pipeline`/`pipeline_run`, job↔dataset correlation (Pipeline pillar).
- **C5 — Incidents, SLAs, alerting** 🟡 *(incidents built)* — first-class incidents with **fingerprint dedup, auto-resolve, and evidence on every alert** (`incidents.py`), raised from rule failures + anomalies; `GET /incidents`, per-monitor/table feeds, `:ack`/`:resolve`; UI incident feed + per-table active incidents. *Next:* SLAs/anomaly budgets, Slack/Teams/PagerDuty routing + mute windows, lineage blast-radius.
- **C6 — Anomaly detection** 🟡 *(baseline built)* — robust **median+MAD rolling baseline** with expected band + graceful degradation on thin history (`anomaly.py`), wired into the volume pillar → anomaly incidents. *Next:* seasonal decomposition, per-pillar coverage, band overlays on charts, feedback-tuned precision.
- **C7 — Data contracts, cost governance, PII/governance hardening** ⬜.

Full detail, data model, and the 14-scenario matrix: [`docs/design/10-production-data-observability.md`](./docs/design/10-production-data-observability.md).

---

## 3. Data Validation roadmap (V-phases)  ← *the newly-detailed track*

Goal: make the compare engine **execute against real systems** and be **usable in the UI**,
covering the production scenarios in §4.2. Each phase is independently shippable and
testable (the `LocalSqlConnector` lets us exercise real SQL paths locally; cross-DB Spark
needs live sources, marked 🔒).

- **V1 — Count & schema compare over live connectors** ✅ *(built)* — `CompareService.run` dispatches count/schema/data; count (tolerance-aware) + schema (canonical type match, source/target-only, drift) over live connectors; `POST /compares/{id}:run`, `GET /compares`. Locally testable via two `local` connections.
  - `count_compare`: `SELECT COUNT(*) [WHERE filter]` each side (tolerance-aware ops + %); scalars only.
  - `schema_compare`: pull both schemas, normalize to canonical types, report present-on-both / source-only / target-only, type/nullability/ordinal match.
  - Wire into `CompareService` (`run_count` / `run_schema`), reusing `typemap`.
  - **Locally testable** via two `LocalSqlConnector` connections (no warehouse).
  - API already exists (`POST /compares` with `kind`); add execution + result.

- **V2 — Live key & column auto-detection** ⬜
  - Promote `keydetect` to the live two-tier method: keyable-type filter → name-priority → **sample (~10k) greedy composite** → **source-validate by pushdown** (`COUNT(*)=COUNT(DISTINCT key)`).
  - Column auto-selection: intersection, mapping, per-side excludes, "likely-not-comparable" flags.
  - Return detected key + confidence + how validated; user override.

- **V3 — Data compare (cell-level)** ✅ *(local/pull built)* / ⬜🔒 *(pushdown EXCEPT + Spark scale pending)* — real cell-level diff via row-pull + the tested `compare_rows` engine, with **minimal-key auto-detection** (distinctness-ranked, source-validated — datadiff-style); matched/mismatch/src-only/dst-only + per-column counts + paged `differences`. *Next (V4):* server-side EXCEPT fast-path (same account) + Spark for cross-DB scale.
  - Same-Snowflake-account **server-side EXCEPT** fast-path (counts, then bounded sample).
  - Same-dialect **pushdown EXCEPT + keyed FULL OUTER JOIN / IS DISTINCT FROM** to attribute differing columns.
  - **Locally testable** end-to-end against `LocalSqlConnector` (SQLite supports EXCEPT + joins) — real SQL, no mocks.
  - Feed results through the existing summary→DB / detail→ResultStore plumbing + `differences` drill-down.

- **V4 — Spark cross-DB (Oracle ↔ Snowflake)** ⬜ 🔒
  - `SPARK_HASH_GREEDY` (general) + `SPARK_ADAPTIVE_RANGE` (integer-keyed large tables): partitioned JDBC reads, canonical normalization, key-group hash dedup, greedy within-key matching, cell diffs, bounded samples, difference-tolerance %.
  - Same semantics as the pure `compare_rows` (already tested) → correctness anchored by the in-memory engine.

- **V5 — Compare UI + batch/orchestration** 🟡 *(UI built)* — Data Validation section: create+run compare (count/schema/data), job list with match badges, and a detail view with summary cards + **cell-level diff grid** (src→dst highlighting) / schema-drift table. *Next:* batch of table pairs with continue-on-error `partial`, scheduled recurring compares.
  - **New Data Validation section** in the React app: create compare (pick source/target connection + table(s), key/column config), run, watch status.
  - **Diff grid**: summary cards (match rate, src-only/dst-only/mismatch, per-column counts) + paged cell-level drill-down (side-by-side, differing cells highlighted), filter by column/status.
  - **Batch** of table pairs with continue-on-error → `partial`; schedule recurring compares (reuses the scheduler).

- **V6 — Data residency, cost & contracts link** ⬜
  - **Snowflake-native ResultStore**: write cell diffs/samples to a results schema **inside the customer's Snowflake** (data never leaves their perimeter); UI queries them back. Shared with observability bad-record samples.
  - Per-query cost tagging (`dvp:{group}:compare:{job}`); fail-fast mode.
  - Link validation to **data contracts** (C7): a failed compare / schema drift opens an incident (C5).

Full engine design: [`docs/design/05-data-testing.md`](./docs/design/05-data-testing.md).

### 3.1 Real-world data-validation scenarios (what V-phases must handle)
| # | Scenario | Needs |
|---|----------|-------|
| V-S1 | **Migration validation** Oracle → Snowflake (parity before cutover) | schema + full cell compare cross-DB (V3/V4), key detect (V2) |
| V-S2 | **ETL regression** — compare output before/after a pipeline change | data compare same-dialect pushdown (V3), tolerance |
| V-S3 | **Cross-env parity** (prod vs staging same table) | server-EXCEPT fast-path (V3) |
| V-S4 | **Reconciliation** — row counts tie across layers/systems | count compare + tolerance (V1) |
| V-S5 | **Schema-drift gate** in CI before promote | schema compare (V1), contract link (V6) |
| V-S6 | **Large tables** (100M+ rows) without blowing memory/cost | adaptive range + partitioned reads + sampling (V4) |
| V-S7 | **Type/representation differences** (NUMBER(38,0) ↔ DECIMAL, VARCHAR2 ↔ VARCHAR, TZ) | canonical type normalization ✅ (V-core) |
| V-S8 | **Composite / no obvious key** | sample greedy composite + source-validate (V2) |
| V-S9 | **One bad table in a 200-table batch** | continue-on-error `partial` (V5) |
| V-S10 | **Sensitive data can't leave the account** | Snowflake-native ResultStore (V6) |
| V-S11 | **Recurring nightly reconciliation** | scheduled compares (V5) |
| V-S12 | **Column renames / mappings** between systems | column mapping + per-side excludes (V2) |

---

## 4. Cross-cutting / shared roadmap

| Area | Now | Next |
|------|-----|------|
| **Connectors** | Snowflake (needs creds), Inline, LocalSql | Oracle (JDBC + dialect), Postgres, SQL Server, BigQuery — one `DialectAdapter` each |
| **Scheduler/orchestration** | interval runner (opt-in) | cron precision, multi-tenant fan-out, job queue backed (arq), retries, cost caps |
| **ResultStore / residency** | local FS | object-store (S3/MinIO) + **Snowflake-native** (shared by observability samples + compare diffs) |
| **Incidents & alerting** | model stub | lifecycle + SLA/anomaly budget + routing/dedup/mute + Slack/email (C5) |
| **Anomaly detection** | static thresholds | rolling/seasonal baselines, bands, ML/SPRT (C6) |
| **Security / PII / governance** | tenancy + audit | PII tagging + sample redaction, RLS on Postgres, secret backends (vault/keyring) |
| **Cost** | — | per-run bytes/credits on `monitor_run`/`compare_job`, query tagging, caps |
| **MCP** | stub | expose monitors/rules/compares as MCP tools (`docs/design/06`) |
| **Deployment** | local `dvp run` | server/prod Docker, Postgres, Spark, CI gates |

---

## 5. Suggested sequencing (what to do next)

Prioritized for "make it real + demoable + enterprise-credible":

1. **C3 — Catalog/lineage + Overview** *(observability's big-picture UX; the medallion heatmap the customer described)*, **or**
2. **V1 + V3 — Count/schema compare + local data compare** *(stands up the whole second product end-to-end locally, demoable immediately via `LocalSqlConnector`)*.
3. **V5 — Compare UI** right after V1/V3 so validation is usable, not just API.
4. **C5 — Incidents/SLAs/alerting** *(turns both products' red states into action)*.
5. **Oracle connector + V4 Spark** *(unlocks the headline Oracle↔Snowflake migration-validation use case)* 🔒.
6. **C6 anomaly detection**, **V6 residency**, **C4 pipelines**, **C7 contracts**.

> Recommendation: do **V1+V3+V5** next to light up Data Validation as a working, visible
> second product (all locally testable), unless the priority is the observability
> Overview (C3). Both are high-leverage; pick based on which demo matters more.

---

## 6. Data model status

Built (`dvp.db.models`): `organization`, `group`, `user`, `membership`, `connection`,
`monitor` (+watermark), `dq_rule`, `monitor_run`, `rule_result` (+table_ref, ad-hoc),
`metric`, `incident` (stub), `compare_job`, `compare_result`, `catalog_table`, `audit_log`.

Planned (design `07` + `10 §4`): `source`, `dataset`/`dataset_column`/`layer`,
`lineage_edge`, `pipeline`/`pipeline_run`, `rule_binding`, `metric_baseline`, `segment`,
`sla`/`slo`, `alert_channel`/`alert_route`, `data_contract`.

---

## 7. Testing strategy status

Three layers, **no mocks** (golden rule #1): **pure unit** · **API E2E** (real in-memory
stack via Inline/LocalSql connectors) · **functional** (real HTTP).

- Observability: 50+ pytest + `tests/scripts/test_observability_features.sh` (25 checks: watermark, pillars, custom rules, pushdown run). Scenario matrix S1–S14 in `docs/design/10 §10` (S9 custom-rule, profiling, evaluator covered; S1/S4/S5/S8/S11–S14 pending with their features).
- Validation: `test_typemap`-equivalents via `test_rowcompare`, `test_keydetect`, `test_strategy`, `test_compare_service` (pure engine + job lifecycle). **Gap:** live-execution E2E (V1/V3 add local-SQL functional tests; V4 needs live sources).

Each new phase ships: pure tests + API E2E (local connectors) + a functional-script section.

---

## 8. Reference index

**Design docs:** [00 overview](./docs/design/00-overview.md) · [01 architecture](./docs/design/01-architecture.md) · [02 multi-tenancy](./docs/design/02-multi-tenancy.md) · [03 snowflake](./docs/design/03-snowflake-connectivity.md) · [04 observability](./docs/design/04-observability.md) · [05 data-testing](./docs/design/05-data-testing.md) · [06 mcp](./docs/design/06-mcp.md) · [07 data-model](./docs/design/07-data-model.md) · [08 api](./docs/design/08-api.md) · [09 testing-strategy](./docs/design/09-testing-strategy.md) · [10 production observability](./docs/design/10-production-data-observability.md)

**Key source:** observability `src/dvp/observability/*` · validation `src/dvp/testing_engine/*` · connectors `src/dvp/connectors/*` · API `src/dvp/api/routers/*` · UI `frontend/src/*`

**Jira (UI tracking):** DK-4900 (watermark UI), DK-4901 (four-pillar UI).

**Run it:** backend `PYTHONPATH=src .venv/bin/dvp run` (:8000) · UI `cd frontend && npm run dev` (:5173).

---

## 9. Changelog (what shipped when)

- **2026-10-07 (late)** — **Oracle connector** (python-oracledb thin; OWNER.TABLE, ROWNUM sampling, ALL_TAB_COLUMNS) + **File/S3 connector** (CSV/JSON/NDJSON/Parquet, local or `s3://`, loaded into embedded SQLite). **Source-independence refactor**: each connector owns its `dialect`; removed the source_type→dialect map so the engine has zero per-source branching — proven by running count/schema/data/DQ on a CSV file. **UI connection setup** (Oracle/Snowflake/File/S3 forms + Test/Delete). 86 pytest.
- **2026-10-07 (eve)** — **Data compare → four buckets** (datadiff-aligned): bucket 1 value_difference (1:1 key, cells differ), 2 source_only, 3 target_only, 4 ambiguous (duplicate-key many-to-many, set aside); **pass-record samples + capped bad-record samples per bucket**; `GET /compares/{id}/differences?bucket=` tabbing; four-bucket Compare UI with pass/diff/only/ambiguous tabs; count-compare **tolerance** surfaced in UI. Studied datadiff `DataCompareUtil` directly for the bucket semantics (`srcTargetSingleKeyMisMatchCount` etc.).
- **2026-10-07 (pm)** — **Data Validation live** (V1 count/schema + V3-local cell-level data compare with minimal-key auto-detect; `LocalSqlConnector` with real rows) + **Compare UI** (V5: list/create/run + diff grid). **Incidents + anomaly detection** (C5/C6): robust median+MAD baseline (`anomaly.py`), first-class incidents with dedup/auto-resolve/evidence (`incidents.py`), wired into capture paths; Incidents UI + per-table feed. **Competitive research** (4 agents: datadiff, DMS, dmsspark, market scan) → strategy §0.5. **Seed data** (`scripts/seed_demo.py`): medallion tables, 14-day history with a volume anomaly, failing rules, and real compares. Migrations 0002/0003. 72 pytest, 35 routes.
- **2026-10-05/07** — Observability **C1** (custom DQ rules + metrics) and **C2** (pushdown evaluator, `MonitorRunner`, opt-in scheduler, `LocalSqlConnector`). React UI: monitor create w/ watermark detect/validate, table observability (4 pillars + history + column drill-down + charts-by-default), Rules panel (add/evaluate/record + history). Fixed SQLite-concurrency segfault (serialized sessions) + local uvicorn loop. Design doc `10` + this roadmap.
- **Earlier scaffold** — platform foundation, observability monitors/rules/autogen/pillars, validation compare engine (typemap/strategy/keydetect/rowcompare/service) + API, MCP stub, design docs 00–09.
