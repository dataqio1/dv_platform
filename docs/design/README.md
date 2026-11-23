# DV Platform — Design Docs

A new Python/PySpark **Data Validation Platform** for Snowflake-centric data
observability and cross-database data testing, built multi-tenant for the
enterprise from day one.

> Status: **Draft for review.** Nothing here is built yet. These documents
> describe the intended design. Review comments should be raised per-document.

## Read in this order

| # | Document | What it covers |
|---|----------|----------------|
| 00 | [Overview & Goals](./00-overview.md) | Vision, scope, personas, glossary, non-goals, open decisions |
| 01 | [Architecture](./01-architecture.md) | Components, tech stack, runtime topology, repo layout, compute model |
| 02 | [Multi-Tenancy & Isolation](./02-multi-tenancy.md) | Org / group model, isolation guarantees, RBAC, data separation |
| 03 | [Snowflake Connectivity](./03-snowflake-connectivity.md) | Key-pair auth, SSO without per-request browser, pooling, secrets |
| 04 | [Data Observability](./04-observability.md) | Monitors, DQ rules, auto-generation (3 levels), incremental analysis, dashboards |
| 05 | [Data Testing](./05-data-testing.md) | Count / schema / data compare, key & column detection, cell-level results |
| 06 | [MCP Server](./06-mcp.md) | Model Context Protocol surface so agents can drive the platform |
| 07 | [Data Model](./07-data-model.md) | Metadata schema (ERD), tenancy columns, versioning, retention |
| 08 | [API Design](./08-api.md) | REST surface, conventions, pagination, errors, async jobs |
| 09 | [Testing Strategy](./09-testing-strategy.md) | E2E functional tests (no mocks), test matrix, CI gates |
| 10 | [Production Data Observability](./10-production-data-observability.md) | Production data/execution model, real-world scenarios, custom rules & metrics, roadmap |
| 11 | [Load-Type Detection & Load-Aware DQ Rules](./11-load-type-detection-and-dq-rules.md) | ETL metadata columns (declare once, tool presets), SCD2 (end-date/high-date) config, load-type detection, load-aware rule packs with provenance labels + enable/disable |

## Design principles (the short version)

1. **Tenant isolation is non-negotiable.** A group can never see, query, or
   affect another group's connections, metadata, or results. See [02](./02-multi-tenancy.md).
2. **Push work down to Snowflake.** For single-source observability we run SQL
   *inside* Snowflake and pull back only aggregates. PySpark is reserved for
   cross-database compares (e.g. Oracle ↔ Snowflake) where no shared engine exists.
   See [01](./01-architecture.md#compute-model) and [05](./05-data-testing.md).
3. **Only analyze new data.** Every monitor run processes a *delta* using a
   watermark; the only full-scan exception is uniqueness. See [04](./04-observability.md#incremental-analysis).
4. **Rules are versioned config, editable at runtime.** A run executes an
   immutable snapshot; edits apply to the next run without stopping anything.
5. **Fail loudly.** Non-fatal issues surface as warnings on the response; we
   never silently swallow an integration failure (carried over from the platform
   golden rules).
6. **Everything is testable end-to-end.** Real Snowflake, real Oracle, real
   Postgres in CI — no mocks for external systems. See [09](./09-testing-strategy.md).
7. **Runs anywhere.** Same code on a desktop (in-memory SQLite, zero external
   deps) and on any Linux server (single-node or clustered). Mode is a config
   choice. See [01 §5](./01-architecture.md#5-deployment--runtime-modes).
