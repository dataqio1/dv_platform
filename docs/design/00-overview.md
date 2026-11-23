# 00 — Overview & Goals

## 1. Problem statement

Data teams that have standardized on Snowflake need two things that today live
in separate, heavyweight tools:

1. **Observability** — continuous confidence that the data landing in Snowflake
   is correct, fresh, complete, and within expected bounds, with the ability to
   author and auto-generate data-quality (DQ) rules per table.
2. **Testing / reconciliation** — the ability to prove a migration or pipeline
   is correct by comparing a legacy source (e.g. Oracle) against Snowflake at the
   count, schema, and *cell* level.

We are building a single Python/PySpark platform that does both, is multi-tenant
(so many independent groups inside one organization can use it without seeing
each other), and exposes an MCP interface so AI agents can drive it.

## 2. Goals

- **G1 — Snowflake data observability.** Monitors as first-class objects over a
  table or set of tables; searchable catalog; deep per-table analysis; full
  CRUD on DQ rules at the table level; auto-generation of DQ rules at three
  selectable depths; runtime rule editing; incremental (delta-only) analysis;
  rich charts and dashboards.
- **G2 — Data testing.** Count compare, schema compare, and full data compare
  across Snowflake ↔ Oracle (and any source combination), with automatic
  compare-key and compare-column detection, and cell-level drill-down of
  mismatches.
- **G3 — Multi-tenancy.** Organization → group hierarchy with *complete*
  isolation of connections, metadata, credentials, compute, and results.
- **G4 — Snowflake auth flexibility.** Key-pair *and* SSO, where SSO does **not**
  pop a browser window on every request.
- **G5 — Agent-native.** A first-class MCP server so users' agents can list
  monitors, inspect analyses, trigger compares, and read results.
- **G6 — Enterprise-grade.** Security, auditability, RBAC, secrets management,
  observability of the platform itself, and comprehensive automated tests.
- **G7 — Runs anywhere.** The same code runs on a **desktop** (local, in-memory
  database, zero external dependencies) and on **any Linux server** (single-node
  or clustered) — deployment mode is a config choice, not a code change.

## 3. Non-goals (v1)

- Not building connectors for every warehouse. v1 sources: **Snowflake** and
  **Oracle** (plus flat files for compare). The connector layer is pluggable so
  Postgres/SQL Server/BigQuery can follow.
- Not a data catalog / lineage product. We catalog *monitored* tables, not the
  whole estate, and we do not compute column-level lineage.
- Not an ETL/transformation tool. We observe and compare; we do not move data.
- Not building our own IdP. Platform login federates to the org's existing SSO
  (OIDC/SAML).

## 4. Personas

| Persona | Needs |
|---------|-------|
| **Data Engineer** | Create monitors, tune/auto-gen DQ rules, wire up connections, run reconciliations during migrations. |
| **Data Analyst / Steward** | Search for a table, read its quality analysis, triage failing rules, drill into mismatched cells. |
| **Group Admin** | Manage their group's members, connections, and secrets — nothing outside their group. |
| **Org Admin** | Create groups, set org-wide policy, view billing/usage. Cannot read group data by default. |
| **AI Agent (via MCP)** | Programmatically list monitors, fetch analyses, start compares, read results on behalf of a user, honoring that user's group scope. |

## 5. Core concepts & glossary

| Term | Definition |
|------|------------|
| **Organization (tenant)** | Top-level customer boundary. Hard isolation boundary. |
| **Group (workspace)** | A team within an org. The *primary* isolation and ownership unit — every connection, monitor, rule, and run belongs to exactly one group. "Completely independent from others" = enforced at the group level. |
| **Connection** | Credentials + coordinates for a data source (Snowflake account, Oracle DB). Owned by a group. |
| **Monitor** | A first-class object watching one or more tables in a Snowflake schema. Owns DQ rules, a run schedule, a watermark, and a history of runs. See [04](./04-observability.md). |
| **DQ Rule** | A versioned assertion about a table/column (e.g. `null_rate(email) < 1%`). Evaluated per monitor run. |
| **Monitor Run** | One incremental evaluation of all a monitor's rules against the new data since the last watermark. Produces metrics, rule results, and incidents. |
| **Incident** | A rule breach (or drift) raised by a run, with severity and lifecycle (open → acknowledged → resolved). |
| **Compare Job** | A data-testing task: count, schema, or data compare between two datasets. See [05](./05-data-testing.md). |
| **Watermark** | The high-water mark (column + value) marking how far a monitor has analyzed, so the next run only reads newer rows. |

## 6. High-level capability map

```
                         DV Platform (multi-tenant)
    ┌───────────────────────────────────────────────────────────────┐
    │  Observability (G1)              Data Testing (G2)             │
    │  ─────────────────               ────────────────             │
    │  • Monitors (1st class)          • Count compare              │
    │  • Table search + deep analysis  • Schema compare             │
    │  • DQ rule CRUD                  • Data compare (cell level)  │
    │  • Auto-gen rules (3 levels)     • Auto key/column detection  │
    │  • Runtime rule editing          • SF ↔ Oracle ↔ any          │
    │  • Incremental (delta) analysis                               │
    │  • Charts & dashboards                                        │
    ├───────────────────────────────────────────────────────────────┤
    │  Platform: Multi-tenancy · Snowflake auth (key-pair + SSO) ·  │
    │            RBAC · Secrets · Scheduling · MCP server · Audit   │
    └───────────────────────────────────────────────────────────────┘
```

## 7. Decisions (resolved + open)

Status reflects reviewer input to date. ✅ = decided, 🟡 = default chosen, details
pending.

| # | Decision | Resolution | Where |
|---|----------|------------|-------|
| D1 | Metadata store tenancy model | ✅ **Shared Postgres, row-level `group_id` + Postgres RLS**; schema-per-group for high-isolation tier | [02](./02-multi-tenancy.md#31-metadata-isolation) |
| D2 | Platform login auth | ✅ **SaaS uses enterprise SSO (OIDC; IdP TBD)**. 🟡 For current bring-up, **desktop mode runs with no password** (single local user). | [02](./02-multi-tenancy.md#9-platform-authentication) |
| D3 | Snowflake SSO ("no browser per request") | ✅ **Snowflake OAuth (auth-code + refresh token), cached per user/group**; `externalbrowser`+cached ID token as desktop fallback | [03](./03-snowflake-connectivity.md#4-sso-without-a-browser-per-request) |
| D4 | Async job runtime | ✅ **Spark for compute (standalone or K8s); Arq (Redis) for orchestration** | [01](./01-architecture.md#5-deployment--runtime-modes) |
| D5 | Where observability profiling runs | ✅ **SQL pushdown into Snowflake**; PySpark only for cross-DB compare | [01](./01-architecture.md#3-compute-model) |
| D6 | Hosting | ✅ **Any Linux server** — Compose single-node; K8s optional; no hard cloud dependency | [01](./01-architecture.md#5-deployment--runtime-modes) |
| D7 | Runtime modes | ✅ **Desktop/local (in-memory SQLite)**, single-server, clustered — config-selected | [01](./01-architecture.md#5-deployment--runtime-modes) |
| D8 | **Deployment / ownership model** | ✅ **SaaS: one deployment, many customer orgs.** Multi-tenant isolation is first-class; desktop mode is a dev/single-user convenience. | [02](./02-multi-tenancy.md) |
| D9 | **Where results & bad-record samples are stored** | ✅ **Pluggable `ResultStore`.** v1 default: summary in Postgres + detail in object store/local. **Roadmap/config: store results inside the customer's own Snowflake** so data never leaves their account. Configurable per group. | [05 §8](./05-data-testing.md#8-result-structure-cell-level-drill-down-2c), [01 §6.1](./01-architecture.md#61-pluggable-infrastructure-what-makes-the-three-modes-possible) |
| D10 | Monitoring freshness | ✅ **No real-time.** Configurable hourly/daily cron; **high-water-mark incremental is the primary path**, Snowflake Streams optional. | [04 §4](./04-observability.md#4-incremental-analysis--only-new-data-1g) |
| D11 | Scale target | ✅ **Hundreds of monitored tables per group**; must scale horizontally. | [01 §6.2](./01-architecture.md#62-scaling-to-hundreds-of-tables) |
| D12 | v1 UI scope | 🟡 **Full web UI in v1** (interpreting your "Yes"); confirm if you'd prefer API+MCP first. | [08](./08-api.md) |

### Still pending from you (non-blocking; sensible defaults in place)
- **IdP details** for SaaS SSO (Okta / Entra / Ping …) — you'll provide later.
- **Snowflake edition / External OAuth** availability (for the no-browser service path).
- **Oracle** version + auth (wallet vs user/password) + network reachability.
- Confirm **v1 sources = Snowflake + Oracle** only.
