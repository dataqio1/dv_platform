# DV Platform

Multi-tenant **data observability** and **data testing** platform for Snowflake,
built in Python / PySpark. Runs on a desktop (in-memory, zero external deps) or on
any Linux server (single-node or clustered).

> **Design docs:** see [`docs/design/`](./docs/design/README.md) for the full
> architecture. This repo is the scaffold that implements that design.

## What's here

| Area | Module | Status |
|------|--------|--------|
| Config & runtime modes | `dvp.config` | ✅ local / server / prod profiles |
| Tenancy & RBAC | `dvp.core.tenancy`, `dvp.services.tenancy` | ✅ context, roles, isolation |
| Metadata store | `dvp.db` | ✅ SQLAlchemy models + SQLite/Postgres |
| Pluggable adapters | `dvp.abstractions` | ✅ Secret / Result / Job / Spark / Identity |
| Connections | `dvp.services.connections` | ✅ CRUD + secret split |
| Observability | `dvp.observability` | ✅ rules, 3-level autogen, CRUD; watermark detect/validate; 4-pillar history + column drill-down |
| Data testing | `dvp.testing_engine` | ✅ type map, key detect, strategy, pure cell-compare |
| API | `dvp.api` | ✅ FastAPI, ApiResponse, problem+json |
| MCP | `dvp.mcp` | ✅ tool router; stdio server (optional SDK) |
| Connectors (Snowflake/Oracle) | `dvp.connectors` | 🟡 protocols defined; drivers are optional extras |

Items marked 🟡 are interfaces with the real driver/engine work deferred (they
require live Snowflake/Oracle/Spark). The platform **boots and is fully testable**
without them.

## Quickstart (desktop / local — no external dependencies)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# Run the API with an in-memory SQLite DB and a no-password local user:
dvp run                      # then open http://127.0.0.1:8000/docs

# Or run the tests:
pytest
```

### Web UI (React + Vite)

A SPA for the Data Observability features lives in [`frontend/`](./frontend). With the
API running, start it with `cd frontend && npm install && npm run dev` and open
http://127.0.0.1:5173 (it proxies `/api` to the backend). Covers monitor creation with
**watermark auto-detect/validate** and the per-table **four-pillar** page (freshness,
volume, schema, quality) with history and column drill-down. See
[`frontend/README.md`](./frontend/README.md).

Example calls (local mode auto-provisions one org/group/user):

```bash
curl -s localhost:8000/api/v1/health | jq
curl -s localhost:8000/api/v1/whoami | jq
curl -s -X POST localhost:8000/api/v1/connections \
  -H 'content-type: application/json' \
  -d '{"source_type":"snowflake","name":"sf","coordinates":{},"auth_method":"key_pair"}' | jq
```

## Runtime modes

Select with `DVP_PROFILE` (see `.env.example`):

- `local` — in-memory SQLite, in-process jobs, no-password identity, `local[*]` Spark.
- `server` — Postgres + Redis + Spark on one Linux host (Docker Compose).
- `prod` — HA Postgres/Redis, Spark cluster/K8s, enterprise SSO.

Everything behind `dvp.abstractions.factory.Container` is swapped by mode; business
logic is identical across all three (see
[docs/design/01 §5](./docs/design/01-architecture.md#5-deployment--runtime-modes)).

## Development

```bash
ruff check src tests
black --check src tests
mypy
pytest
```

## License

Proprietary — DataQ.
