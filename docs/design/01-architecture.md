# 01 — Architecture

## 1. Component overview

```
                                 ┌────────────────────────────┐
   Browser / Agent  ───────────► │   API Gateway / Ingress    │ (TLS, WAF, rate limit)
                                 └──────────────┬─────────────┘
                                                │
        ┌───────────────────────────────────────┼──────────────────────────────────┐
        ▼                                        ▼                                  ▼
 ┌─────────────┐                        ┌────────────────┐                 ┌────────────────┐
 │   Web UI    │   (Next.js, SSR)       │   API service  │  (FastAPI)      │   MCP server   │
 │  dashboards │ ◄───── REST/WS ──────► │  auth, RBAC,   │ ◄── in-proc ──► │ (tools/        │
 │  & charts   │                        │  CRUD, jobs    │    or sidecar   │  resources)    │
 └─────────────┘                        └───────┬────────┘                 └────────────────┘
                                                │ enqueue
                                                ▼
                                       ┌──────────────────┐
                                       │  Job orchestrator │  (Arq workers on Redis)
                                       │  schedule + fan-out│
                                       └───────┬───────────┘
                          ┌────────────────────┼─────────────────────┐
                          ▼                     ▼                     ▼
                 ┌─────────────────┐   ┌──────────────────┐  ┌──────────────────┐
                 │ Observability   │   │ Data-Testing      │  │  Rule auto-gen    │
                 │ engine          │   │ compare engine    │  │  (profiler)       │
                 │ (SQL pushdown)  │   │ (PySpark on K8s)  │  │                   │
                 └───────┬─────────┘   └────────┬─────────┘  └────────┬─────────┘
                         │                      │                     │
        ┌────────────────┼──────────────────────┼─────────────────────┼────────────┐
        ▼                ▼                       ▼                     ▼            ▼
 ┌────────────┐  ┌───────────────┐      ┌───────────────┐    ┌──────────────┐  ┌─────────┐
 │ Metadata DB│  │ Secrets (Vault│      │  Snowflake    │    │    Oracle    │  │ Object  │
 │ (Postgres) │  │ / AWS SM)     │      │  (per group)  │    │  (per group) │  │ store   │
 └────────────┘  └───────────────┘      └───────────────┘    └──────────────┘  └─────────┘
```

### Services

| Service | Responsibility | Tech |
|---------|----------------|------|
| **API service** | AuthN/Z, tenant context, CRUD of monitors/rules/connections, job submission, result serving, WebSocket for live run status. | FastAPI (Python 3.12), Pydantic v2, SQLAlchemy 2.x async, Uvicorn |
| **Web UI** | Dashboards, charts, table search, rule editor, compare drill-down. | Next.js (App Router), React, server components, Recharts/ECharts |
| **MCP server** | Exposes platform capabilities to agents as MCP tools/resources, scoped to the caller's group. | Python MCP SDK; runs as sidecar or separate deploy. See [06](./06-mcp.md) |
| **Job orchestrator** | Cron scheduling of monitor runs, fan-out, retries, backpressure, concurrency limits per group. | Arq (async Redis), APScheduler-style cron, or Celery-beat equivalent |
| **Observability engine** | Builds & runs profiling / rule SQL *inside* Snowflake, reads back aggregates, evaluates rules, writes metrics & incidents. | Python, Snowflake connector, Jinja-templated SQL |
| **Compare engine** | Count/schema/data compare; key & column detection; cell-level diffs. PySpark for cross-DB. | PySpark 3.5 on Kubernetes (Spark-on-K8s / Spark Connect) |
| **Metadata DB** | All platform state (tenants, groups, connections*, monitors, rules, runs, incidents, compare jobs). | PostgreSQL 16 + Alembic migrations |
| **Secrets store** | Connection credentials, key-pair private keys, OAuth refresh tokens — encrypted, never in the metadata DB. | Pluggable `SecretStore`: local encrypted keystore / OS keyring (desktop), HashiCorp Vault, or a cloud secrets manager (server/prod) |
| **Object store** | Large compare result artifacts, bad-record samples, exported reports. | S3 (per-group prefix, SSE-KMS) |

\* Connection *coordinates* live in Postgres; connection *secrets* live in the secrets store, referenced by ID.

## 2. Tech stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Language | **Python 3.12** | Requested; best Snowflake/Spark/MCP ecosystem. |
| Compute | **PySpark 3.5** | Requested; needed for cross-DB cell-level compare at scale. |
| API framework | **FastAPI** | Async, first-class OpenAPI, Pydantic validation, easy to test. |
| Data validation | **Pydantic v2** | Request/response models, config, rule schemas. |
| ORM / migrations | **SQLAlchemy 2.x (async) + Alembic** | Mature, async, DB-agnostic so the same models run on SQLite (local) and Postgres (server/prod). |
| Metadata DB | **PostgreSQL 16** (server/prod) · **in-memory SQLite** (local) | Postgres gives RLS + JSONB for tenancy at scale; SQLite gives a zero-dependency desktop mode. Same schema via SQLAlchemy. |
| Queue / workers | **Redis + Arq** (server/prod) · **in-process async** (local) | Lightweight async job queue when distributed; a local task runner when single-process. |
| Snowflake | **snowflake-connector-python** + **snowflake-sqlalchemy** | Key-pair, OAuth, external-browser; pushdown SQL. |
| Oracle | **oracledb** (thin mode) + JDBC for Spark | No Oracle client install needed for metadata ops. |
| Secrets | **`SecretStore`** — local keystore (desktop) / Vault / cloud secrets manager | Never store raw creds in the app DB; adapter chosen per mode. |
| Packaging | **uv** + `pyproject.toml`; Docker (multi-stage, distroless/Corretto-style slim) | Fast, reproducible. |
| Lint/format/type | **ruff + black + mypy (strict)** | Enterprise code hygiene. |
| Tests | **pytest**, **testcontainers**, real Snowflake/Oracle test accounts | No mocks for externals (golden rule). See [09](./09-testing-strategy.md). |

## 3. Compute model

This is the most important architectural decision, so it gets its own section.

**Principle: run the computation where the data already is.**

### 3.1 Observability (single Snowflake source) → SQL pushdown

For monitors, both the data and a capable engine (Snowflake) are in one place.
We do **not** pull rows into Spark. The observability engine:

1. Renders rule/profile logic as a single (or few) aggregate SQL statement(s)
   using Jinja templates — e.g. one `SELECT` computing null counts, min/max,
   distinct counts, row counts, and rule predicates across all columns in the
   delta partition.
2. Executes it in Snowflake using the group's warehouse.
3. Reads back only the **aggregates** (kilobytes), evaluates rule pass/fail in
   Python, and writes metrics + incidents to Postgres.

Benefits: near-zero data egress, uses the customer's compute (cost transparency),
scales with their warehouse, and is fast. This mirrors datadiff's proven
"push `COUNT(DISTINCT …)` / `EXCEPT` into the DB, only scalars cross the wire"
strategy.

### 3.2 Data testing (two different sources) → PySpark

For Oracle ↔ Snowflake there is no shared engine, so we use PySpark:

- **Count & schema compare**: pushdown `COUNT(*)` / `DESCRIBE` to each side; only
  scalars/metadata cross the wire. No Spark cluster needed for these — the API
  worker can do them directly.
- **Data compare**: PySpark reads both sides (with predicate/range pushdown via
  JDBC partitioning), normalizes types, and performs the hash/greedy cell-level
  compare described in [05](./05-data-testing.md). When *both* sides are the same
  Snowflake account, we short-circuit to a server-side `EXCEPT` fast-path and
  skip Spark entirely.

### 3.3 Spark execution

Spark jobs run on **Kubernetes** (Spark-on-K8s operator or Spark Connect). Each
job runs with a service identity scoped to the requesting group; credentials are
injected at launch from the secrets store and never baked into images. Jobs are
ephemeral — one driver+executors per compare job — so a slow/huge compare in one
group cannot starve another (subject to per-group concurrency quotas).

## 4. Repository layout (monorepo)

```
dv_platform/
├── pyproject.toml                # uv workspace root
├── docs/design/                  # these documents
├── packages/
│   ├── core/                     # shared: config, tenancy ctx, errors, logging, ApiResponse
│   │   └── dvp_core/
│   ├── connectors/               # source connectors (pluggable)
│   │   └── dvp_connectors/
│   │       ├── base.py           # Connector, DialectAdapter protocols
│   │       ├── snowflake/        # auth (key-pair, oauth, ext-browser), pooling, SQL gen
│   │       └── oracle/
│   ├── observability/            # monitors, rules, profiler, auto-gen, watermark, incidents
│   │   └── dvp_observability/
│   ├── testing_engine/           # count/schema/data compare, key & column detection
│   │   └── dvp_testing/
│   ├── secrets/                  # SecretStore abstraction (Vault, AWS SM)
│   └── mcp_server/               # MCP tools/resources
├── services/
│   ├── api/                      # FastAPI app: routers, deps, RBAC, OpenAPI
│   ├── worker/                   # Arq workers + scheduler
│   └── spark_jobs/               # PySpark entrypoints (compare, heavy profiling)
├── migrations/                   # Alembic
├── deploy/                       # Helm charts, Dockerfiles, K8s manifests
└── tests/
    ├── unit/
    ├── integration/              # testcontainers: postgres, redis
    └── e2e/                      # real Snowflake + Oracle; no mocks
```

Rationale: a uv workspace of small importable packages keeps the compute engines
(`observability`, `testing_engine`) usable both from the API and from Spark job
entrypoints without a circular dependency on web concerns.

## 5. Deployment & runtime modes

The platform runs the **same code** in three modes, selected by the active config
profile. Nothing is tied to a specific cloud — it deploys on **any Linux server**.

| Mode | Profile | Who / where | Persistence | Queue | Spark | Secrets |
|------|---------|-------------|-------------|-------|-------|---------|
| **Desktop / Local** | `local` | A single engineer on a laptop/desktop (Windows/macOS/Linux) | **In-memory SQLite** (or a local file) | in-process async (no Redis) | `local[*]` in-process | local encrypted keystore / OS keyring |
| **Single server** | `server` | One Linux box (VM or bare metal), Docker Compose | PostgreSQL container (or embedded) | Redis container | local Spark or a small standalone Spark | Vault container / file keystore |
| **Clustered** | `prod` | Many Linux nodes (optionally Kubernetes) | Managed/HA PostgreSQL | Managed/HA Redis | Spark-on-K8s or standalone Spark cluster | Vault / cloud secrets manager |

### 5.1 Desktop / local mode (in-memory, zero external deps)

Requirement: *"execute on Desktop and/or on server; when running locally use an
in-memory database so there is no issue."*

- **Metadata store = in-memory SQLite** (default) or an on-disk SQLite file if the
  user wants persistence between sessions. No Postgres, no Docker, no network deps
  to start — `dvp run` boots the API + worker + UI in one process.
- Persistence, queue, secrets, and Spark are all chosen behind abstractions
  (§6.1) so switching `local` → `server` → `prod` is a **config change, not a code
  change**. The engines (`observability`, `testing_engine`) are identical in every
  mode.
- **Tenancy in local mode**: SQLite has no Row-Level Security, so isolation falls
  back to the always-present application-layer `group_id` filtering. Local mode is
  effectively single-user/single-group by default (one org, one group), which is
  the common desktop case; multi-group still works logically but without the RLS
  backstop. Multi-tenant *isolation guarantees* (RLS, per-group secret paths) are
  a **server/prod** feature — documented explicitly in
  [02](./02-multi-tenancy.md#8-local-mode-caveat).
- **Compute in local mode**: observability still pushes SQL down to the user's real
  Snowflake; cross-DB compares run on a local Spark session (`local[*]`). Result
  artifacts go to a local directory instead of object storage.
- **MCP in local mode**: served over stdio so a desktop agent (Claude Desktop,
  etc.) connects directly to the local process — no auth server needed.

### 5.2 Single-server mode

- One Linux host, `docker compose up`: API, worker, UI, Postgres, Redis, and
  (optionally) a standalone Spark. Suitable for a team without Kubernetes.
- Reverse proxy (Caddy/nginx/Traefik) for TLS + rate limiting.

### 5.3 Clustered mode

- Multiple Linux nodes; Kubernetes is *optional* (Helm charts provided) but not
  required — the same containers run under Nomad, plain systemd + Compose, or K8s.
- HA Postgres + Redis; Spark jobs on K8s or a standalone Spark cluster, namespaced
  per run; per-group autoscaling and quotas.
- No hard dependency on any one cloud. Cloud building blocks (managed Postgres,
  object storage, secrets manager) are **pluggable adapters**, not assumptions.

### 5.4 Common to all modes

- **Config**: 12-factor; `dvp_core.config.Settings` (Pydantic-settings) with the
  `local` / `server` / `prod` profiles above. One env var picks the mode.
- **Platform observability**: OpenTelemetry traces/metrics, structured JSON logs
  (`structlog`), Prometheus + Grafana in server/prod; console logging in local.
  Health/ready probes on every service.
- **Distribution**: published as Docker images *and* as a `pip install dv-platform`
  package with a `dvp` CLI (`dvp run`, `dvp migrate`, `dvp compare …`) so desktop
  users don't need containers at all.

## 6. Cross-cutting concerns

### 6.1 Pluggable infrastructure (what makes the three modes possible)

Every external dependency sits behind a small abstraction so the runtime mode
(§5) is a config choice, never a code fork:

| Abstraction | `local` | `server` | `prod` |
|-------------|---------|----------|--------|
| `MetadataStore` (SQLAlchemy engine) | in-memory SQLite | Postgres (container) | Postgres (HA) |
| `JobQueue` | in-process async | Redis + Arq | Redis + Arq (HA) |
| `SecretStore` | encrypted local file / OS keyring | Vault / file | Vault / cloud secrets mgr |
| `ArtifactStore` | local filesystem | filesystem / MinIO | S3-compatible object store |
| `ResultStore` | local files + SQLite | Postgres + object store **or Snowflake-native** | Postgres + object store **or Snowflake-native** |
| `SparkSession` | `local[*]` | standalone Spark | Spark-on-K8s / cluster |

The engines depend only on these interfaces, so all tests, the desktop app, and
the clustered deployment exercise the *same* business logic.

**`ResultStore` is a first-class decision (D9).** Cell-level compare diffs and
bad-record samples contain *actual customer data values*. v1 default stores the
summary in Postgres and the heavy detail in the object store, but the interface
has a **Snowflake-native backend** so a group can configure results to be written
to tables in **their own Snowflake account** — meaning sensitive values never
leave their perimeter. This is a per-group config toggle, not a redeploy. See
[05 §8](./05-data-testing.md#8-result-structure-cell-level-drill-down-2c).

- **Tenancy context** is established once per request (see [02](./02-multi-tenancy.md))
  and threaded through every layer; no query runs without a `group_id` scope.
- **ApiResponse<T>** envelope with `data` + `warnings[]` — non-fatal integration
  failures (e.g. a Slack alert failed but the run succeeded) surface as warnings,
  never swallowed (platform golden rule #3).
- **Idempotent migrations** via Alembic; never edit a shipped migration
  (golden rule #2 analogue).
- **Everything auditable**: who created/edited a rule, who ran a compare, who
  read which result — append-only `audit_log` table.

### 6.2 Scaling to hundreds of tables

Target (D11): a group monitors **hundreds of tables**, each with many rules, on
hourly/daily cadence — across many customer orgs in one SaaS deployment. How we
keep that cheap and horizontal:

- **Pushdown + batching keeps warehouse cost flat in rule count.** All of a
  table's rules compile into **one** aggregate SQL statement per run (not one
  query per rule), executed in the customer's Snowflake. 300 rules on a table ≈ 1
  query. See [04 §3.2](./04-observability.md#32-rule-catalog).
- **Monitor fan-out is independent, idempotent work units.** Each (monitor,
  table) run is a self-contained job on the queue; workers scale horizontally and
  pull work. Hundreds of tables = hundreds of small jobs spread across workers,
  not one giant job.
- **Delta-only reads** ([04 §4](./04-observability.md#4-incremental-analysis--only-new-data-1g))
  mean per-run cost scales with *new* data, not table size — a 10B-row table with
  1M new rows/hour only ever scans ~1M rows.
- **Per-group quotas & fair scheduling** prevent one busy group/org from starving
  others; concurrency caps and warehouse mapping are per group ([02 §6](./02-multi-tenancy.md#6-noisy-neighbor--fairness)).
- **Metadata DB stays small**: time-series `metric` table is partitioned by month
  with rollups; heavy detail lives in `ResultStore`/object store, not Postgres
  (see [07](./07-data-model.md#3-key-modeling-decisions)).
- **Catalog refresh is incremental** (only changed tables), driven off
  `INFORMATION_SCHEMA`/`ACCOUNT_USAGE` last-altered timestamps.
- **Spark reserved for cross-DB compares**, which are user-triggered and bounded;
  observability (the high-volume, always-on path) never needs a Spark cluster.
