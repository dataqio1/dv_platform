# 09 — Testing Strategy & Test Cases

> Golden rule (platform-wide, non-negotiable): **write end-to-end functional
> tests with NO mocks for external services.** E2E tests make real calls against
> real Snowflake, real Oracle, and real Postgres. Unit tests may mock, but unit
> tests alone are *not* sufficient.

## 1. Test pyramid (and where mocks are/aren't allowed)

| Layer | Scope | Externals | Mocks? |
|-------|-------|-----------|--------|
| **Unit** | Pure logic: type normalization, key-scoring, rule `to_sql`/`evaluate`, watermark math, strategy selection | none | mocks OK for collaborators |
| **Integration** | Service ↔ real Postgres/Redis; RLS; migrations | Postgres + Redis via **testcontainers** | **no external mocks** |
| **E2E / functional** | Full flow: API → service → engine → **real Snowflake/Oracle** → DB → response | real Snowflake + Oracle test accounts | **NO mocks** |
| **Contract** | OpenAPI ↔ clients ↔ MCP tool schemas stay in sync | generated | n/a |
| **Load/perf** | Large-table compares, many-rule monitors, concurrency/isolation under load | real warehouse | no |

### Test infrastructure
- **Postgres/Redis**: `testcontainers` spin up real instances per test session.
- **Snowflake**: a dedicated CI Snowflake account/warehouse (`DVP_CI`), ephemeral
  databases per test run (`CI_{run_id}`), torn down after. Key-pair + an OAuth test
  integration configured for the SSO tests.
- **Oracle**: `testcontainers` Oracle XE (or a CI Oracle instance) seeded with
  fixtures that mirror Snowflake fixtures for compare tests.
- **Fixtures as code**: seed datasets defined in SQL/Parquet, loaded into both
  Snowflake and Oracle so compares have known-correct expected diffs.
- E2E tests follow the platform convention of also being runnable as shell
  scripts under `tests/e2e/scripts/test_<feature>.sh` that curl the deployed API
  and assert on real responses.

## 2. Functional test matrix — Observability

Each is an E2E test hitting real Snowflake.

### Monitors (1a–1c)
- **OBS-01** Create a single-table monitor → 201, monitor is first-class (has id,
  appears in list, has URL/MCP resource).
- **OBS-02** Create a multi-table monitor (explicit list *and* wildcard pattern) →
  each table analyzed independently, monitor rolls up one health status.
- **OBS-03** Search catalog for a table by name/tag → returns it with
  monitored/unmonitored flag; "create monitor" path works.
- **OBS-04** Deep analysis of a seeded table → profile (null rates, distinct,
  min/max, top-N) matches known fixture values within tolerance.
- **OBS-05** Pause/resume/archive lifecycle transitions behave and are audited.

### DQ rule CRUD (1d)
- **OBS-10** Create each rule type (not_null, unique, range, in_set, regex,
  freshness, custom_sql, cross-column) → persisted, evaluable.
- **OBS-11** Read/list/update/delete a rule → update bumps `version`, delete is
  soft, history preserved.
- **OBS-12** Update a rule's threshold → next run uses new threshold; audit shows
  who/when/old→new.

### Auto-generation, 3 levels (1e)
- **OBS-20** Autogen level 1 on a seeded table → produces exactly the essential
  set (row_count>0, not_null on fully-populated cols, freshness, schema guard);
  no noise.
- **OBS-21** Autogen level 2 → superset of L1 + uniqueness on the true key +
  in_set for the low-cardinality column + numeric ranges matching seeded p1–p99.
- **OBS-22** Autogen level 3 → superset of L2 + distribution/anomaly + regex
  inference (seed an email column) + cross-column consistency.
- **OBS-23** Generated rules land as `suggested` with confidence; accepting them
  enables them; re-running autogen doesn't overwrite human-edited rules.
- **OBS-24** Thresholds are data-derived (not hardcoded): change the fixture's
  null rate, re-autogen → threshold shifts accordingly.

### Runtime rule editing (1f)
- **OBS-30** Start a long run, edit a rule mid-run → in-flight run finishes on the
  old snapshot; `ruleset_snapshot` proves it; the edit applies to the next run.
- **OBS-31** Disable a rule mid-run → next run skips it; current run unaffected.
- **OBS-32** Historical run results remain reproducible after many rule edits
  (each result references the exact rule version).

### Incremental / delta-only analysis (1g) — critical
- **OBS-40** Seed table, run monitor → watermark advances to max HWM value.
- **OBS-41** Insert new rows only → next run's `rows_analyzed` == new rows only;
  old rows are NOT re-read (assert via QUERY_HISTORY predicate / row count).
- **OBS-42** No new rows since last run → run is a no-op delta (0 rows analyzed),
  completes fast, watermark unchanged.
- **OBS-43** A run that fails does **not** advance the watermark → the same delta
  is reprocessed next run (no data skipped, at-least-once).
- **OBS-44** Rows arriving *during* a run are not skipped (watermark set from max
  value actually seen, not wall-clock now).
- **OBS-45 (uniqueness exception)** Insert a row that duplicates an *old* (already
  analyzed) row → the uniqueness rule still catches it, proving uniqueness scans
  beyond the delta; assert it used a pushdown group-by/semi-join, not a full Spark
  pull.
- **OBS-46** Watermark strategies: verify stream-based, CHANGES-based, and HWM-
  column-based each correctly identify the delta on equivalent fixtures.
- **OBS-47** Watermark reset/backfill re-analyzes the requested window and is
  audited.

### Charts / metrics (1h)
- **OBS-50** After N runs, `/metrics` returns coherent time series; chart data
  matches per-run recorded values.

### Incidents & alerting
- **OBS-60** A breach opens an incident (dedup on repeat); recovery for N runs
  auto-resolves it.
- **OBS-61** Alert channel failure → run still `succeeded`, warning surfaced on the
  response (NOT swallowed). This is an explicit golden-rule-#3 test.

## 3. Functional test matrix — Data Testing (real Snowflake ↔ real Oracle)

### Count compare (2a)
- **CMP-01** Equal counts → match. **CMP-02** Different counts → mismatch with
  diff & diff_pct. **CMP-03** Within tolerance → match. **CMP-04** Filtered count.
- **CMP-05** One table errors in a batch → that pair `EXCEPTION`, batch completes
  `partial`.

### Schema compare (2b)
- **CMP-10** Identical schemas → match.
- **CMP-11** Oracle `NUMBER(38,0)` vs Snowflake `NUMBER(38,0)` → canonical match
  (no false mismatch). `VARCHAR2` vs `VARCHAR` → match.
- **CMP-12** Column only on one side → reported source-only/target-only.
- **CMP-13** Type category change (string vs number) → reported mismatch.
- **CMP-14** Nullability / ordinal differences reported.

### Key & column detection (2d)
- **CMP-20** Single-column integer PK auto-detected and source-validated.
- **CMP-21** Composite key detected greedily; confirmed unique via pushdown
  COUNT(DISTINCT).
- **CMP-22** Float/timestamp columns excluded from key candidates.
- **CMP-23** No unique key exists → reported clearly (no garbage compare).
- **CMP-24** Compare-column auto-detection: intersection by default; mapping honored;
  per-side excludes honored; incompatible-type columns flagged/excluded.

### Data compare & cell-level results (2c)
- **CMP-30** Identical data (SF↔Oracle) → 100% match, zero differences.
- **CMP-31** Known cell differences → exact per-cell diffs (column, src, dst) match
  the seeded expectation; `per_column_mismatch` counts correct.
- **CMP-32** Source-only / target-only rows classified correctly.
- **CMP-33** Type normalization: trailing spaces (ignore_spaces), null/""/“null”
  equivalence, numeric tolerance, timezone-normalized timestamps → no false
  mismatches.
- **CMP-34** Same-Snowflake-account pair → server-side EXCEPT fast-path taken
  (assert via QUERY_HISTORY: no Spark job, EXCEPT executed); documented dedupe
  caveat behavior verified.
- **CMP-35** Large integer-keyed table → adaptive range strategy; results correct,
  memory bounded.
- **CMP-36** General SF↔Oracle large compare → Spark hash+greedy path; cell diffs
  correct; bad-record samples bounded to `bad_record_limit`.
- **CMP-37** Cell-level drill-down API paginates and filters by column/status;
  full detail downloadable from S3 artifact.
- **CMP-38** Duplicate rows within a key → greedy matching pairs best matches;
  leftovers classified correctly.

## 4. Multi-tenancy tests (isolation is non-negotiable)

- **TEN-01** Group A cannot list/read/update/delete Group B's monitors, rules,
  connections, runs, or compares → 404/empty, never leakage (test via API *and*
  direct repo call to prove RLS backstops a missing filter).
- **TEN-02** Group A cannot resolve Group B's secrets (SecretStore path assertion).
- **TEN-03** RLS enforced even if the ORM filter is bypassed (raw query with wrong
  `app.current_group` returns nothing).
- **TEN-04** A Spark compare launched by Group A uses only Group A's warehouse/creds
  (assert QUERY_TAG + credential identity).
- **TEN-05** Per-group concurrency quota: Group A saturating jobs does not delay
  Group B's jobs.
- **TEN-06** Cache keys are group-prefixed → no cross-group cache hits.
- **TEN-07** Org-admin break-glass into a group requires explicit action and is
  audited; without it, org-admin cannot read group data.
- **TEN-08** A user in multiple groups: switching active group changes scope; a
  request with a group the user isn't in → 403.

## 5. Snowflake auth tests

- **AUTH-01** Key-pair connect + query succeeds against real Snowflake.
- **AUTH-02** Key-pair dual-key rotation: rotate while queries run → zero failed
  queries.
- **AUTH-03** OAuth SSO: one authorize flow yields a refresh token; **subsequent
  queries open NO browser** (assert connector never invokes the browser opener;
  access tokens refreshed from the stored refresh token). This directly tests the
  "no webpage per request" requirement.
- **AUTH-04** Access-token expiry → transparent refresh (single-flight, no stampede
  under concurrent queries).
- **AUTH-05** Refresh-token revoked/expired → typed `AuthExpiredError` → visible
  incident/warning, not a silent empty result.
- **AUTH-06** Service External-OAuth (no user) path works for scheduled runs with
  no browser ever.
- **AUTH-07** Connection pool reuses sessions per (group, conn, role, warehouse);
  never shares a session across groups.

## 6. MCP tests

- **MCP-01** Every tool respects the session's `TenantContext`; passing a foreign
  `group_id` argument cannot escape scope.
- **MCP-02** Read tools work for Viewer; mutating tools rejected for Viewer,
  allowed for Editor (RBAC parity with REST).
- **MCP-03** `compare_data`/full-scan `run_monitor` return a cost estimate and
  require confirm.
- **MCP-04** MCP results equal REST results for the same query (thin-adapter
  guarantee).
- **MCP-05** Async tool returns job id; polling/streaming returns final result.

## 6b. Runtime-mode tests (desktop / server parity)

- **MODE-01** `dvp run` boots on a clean machine with **in-memory SQLite** and no
  Redis/Vault/Docker → API, worker, UI, and stdio MCP all come up; a monitor can
  be created and run against real Snowflake.
- **MODE-02** Alembic migrations apply cleanly on **both** SQLite (local) and
  Postgres (server) from the same models.
- **MODE-03** The same engine test (a compare, a monitor run) passes identically
  in `local` mode (`local[*]` Spark, filesystem artifacts) and `server` mode
  (standalone Spark, object-store artifacts) — proves mode is config-only.
- **MODE-04** `SecretStore`, `JobQueue`, `ArtifactStore`, `MetadataStore`,
  `SparkSession` adapters each pass a shared conformance test suite across all
  implementations.
- **MODE-05** Switching profile `local` → `server` requires **no code change**
  (config/env only); a smoke script asserts the app starts in each.

## 6c. Data-residency / `ResultStore` tests (D9)

- **RES-01** With object-store backend: compare detail + bad-record samples land
  in the group's prefix; `detail_ref` resolves; nothing with real values is stored
  in the metadata DB (scan Postgres rows to prove it).
- **RES-02** With **Snowflake-native** backend configured for a group: results are
  written to tables **in that group's own Snowflake account**, and **no cell values
  leave Snowflake** (assert the platform object store/DB contains only pointers +
  aggregates).
- **RES-03** Switching a group's `ResultStore` backend is config-only (no redeploy);
  subsequent jobs honor the new backend.
- **RES-04** Observability bad-record samples use the same `ResultStore` as compare
  detail (uniform residency across both products).
- **RES-05** Per-group isolation holds for results too: group A cannot read group
  B's `detail_ref` artifacts/tables.

## 7. Non-functional

- **Performance**: compare of a 100M-row table completes within target; monitor
  with 500 rules issues ~1 pushdown query (batched), not 500.
- **Resilience**: kill a Spark executor mid-compare → job recovers/retries;
  kill a worker mid-run → watermark not advanced, safe re-run.
- **Security**: secrets never logged/returned (scan responses & logs); TLS
  enforced; SAST/dependency scan in CI.
- **Migrations**: Alembic up/down on a fresh DB; every migration idempotent;
  RLS policies present on all tenant tables (automated check).

## 8. CI gates

1. `ruff` + `black --check` + `mypy --strict` must pass.
2. Unit + integration (testcontainers) on every PR.
3. E2E against `DVP_CI` Snowflake + Oracle on merge to main and nightly (ephemeral
   DBs, auto-torn-down).
4. Coverage floor (e.g. 85% lines, 100% on type-normalization & watermark logic).
5. Multi-tenancy isolation suite (section 4) is a **required** gate — a failure
   blocks release.
6. Contract check: OpenAPI ↔ generated clients ↔ MCP schemas in sync.
