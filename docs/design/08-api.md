# 08 — API Design

FastAPI, REST + JSON, OpenAPI auto-generated. WebSocket for live run status. The
MCP server ([06](./06-mcp.md)) is a thin adapter over the same service layer.

## 1. Conventions

- **Base**: `/api/v1`. Versioned; breaking changes → `/v2`.
- **Tenant scope**: active group via `X-Group-Id` header (or `/groups/{group_id}/…`
  path prefix for clarity). Auth via `Authorization: Bearer <platform JWT>`. The
  server derives `TenantContext` and rejects any mismatch (403).
- **Response envelope** (`ApiResponse<T>`), carrying non-fatal warnings so
  integration hiccups surface instead of being swallowed (golden rule #3):
  ```jsonc
  { "data": { ... }, "warnings": [ { "code": "ALERT_CHANNEL_FAILED",
      "message": "Slack notify failed; run still succeeded" } ], "meta": { ... } }
  ```
- **Errors**: RFC 9457 problem+json; typed `code`, `message`, `details`, `trace_id`.
- **Pagination**: cursor-based (`?cursor=&limit=`), `meta.next_cursor`.
- **Async**: mutating/expensive operations return `202` + a job resource
  (`/runs/{id}`, `/compares/{id}`); poll or subscribe via WebSocket.
- **Idempotency**: `Idempotency-Key` header on create/run endpoints.

## 2. Endpoint surface

### Connections
```
POST   /connections                         create (coordinates + secret_ref)
GET    /connections                         list (group-scoped)
GET    /connections/{id}
PATCH  /connections/{id}
DELETE /connections/{id}
POST   /connections/{id}:test               health check (typed HealthResult)
POST   /connections/{id}/sso:authorize      begin Snowflake OAuth (returns redirect)
GET    /connections/{id}/sso/callback        OAuth callback → stores refresh token
POST   /connections/{id}/keypair:rotate     rotate key pair (dual-key, zero downtime)
```

### Catalog / search
```
GET    /catalog/tables?query=&connection_id=      search tables (2c / 1c)
POST   /catalog:refresh                            re-scan a connection's metadata
GET    /catalog/tables/{conn}/{db}/{schema}/{tbl}  deep analysis/profile
```

### Monitors
```
POST   /monitors                            create (scope, schedule)
GET    /monitors                            list (filter by status/tag)
GET    /monitors/{id}
PATCH  /monitors/{id}                        edit scope/schedule/status
DELETE /monitors/{id}                        archive
POST   /monitors/{id}:run                    trigger a run now → 202 {run_id}
POST   /monitors/{id}:pause  / :resume
```

### DQ rules (CRUD — 1d)
```
GET    /monitors/{id}/rules
POST   /monitors/{id}/rules                  create a rule
GET    /monitors/{id}/rules/{rule_id}
PATCH  /monitors/{id}/rules/{rule_id}        edit (bumps version; safe mid-run — 1f)
DELETE /monitors/{id}/rules/{rule_id}        soft delete
POST   /monitors/{id}/rules:autogenerate     { level: 1|2|3, tables?: [] }   (1e)
POST   /monitors/{id}/rules:bulk             enable/disable/accept suggested
```

### Runs, metrics, incidents
```
GET    /monitors/{id}/runs                    history
GET    /runs/{run_id}                          status, rule results, metrics
GET    /runs/{run_id}/rules/{rule_id}/samples  bad-record sample (via ResultStore)
GET    /monitors/{id}/metrics?name=&from=&to=  time series for charts (1h)
GET    /incidents?status=&severity=            group incident feed
PATCH  /incidents/{id}                          acknowledge/resolve
WS     /runs/{run_id}/stream                    live progress
```

### Data testing (2a–2d)
```
POST   /compares:count                        { source, target, options } → result
POST   /compares:schema                        → schema diff
POST   /compares:detect-key                    { source, target } → proposed key (2d)
POST   /compares:data                          → 202 { job_id } (async)
GET    /compares/{job_id}                       summary + status
GET    /compares/{job_id}/differences?page=&column=&status=   cell-level drill (2c)
GET    /compares/{job_id}/detail               full cell-level detail (via ResultStore: object store or Snowflake-native, D9)
WS     /compares/{job_id}/stream               live progress
```

### Admin / tenancy
```
POST   /groups                                 (org_admin) create group
GET    /groups                                 groups the caller belongs to
POST   /groups/{id}/members                    (group_admin) add member + role
GET    /groups/{id}/usage                       quotas, spend, run counts
GET    /me                                      whoami: user, active group, role, perms
```

## 3. Validation & security

- Every request body is a Pydantic model; invalid input → `422` with field errors.
- RBAC enforced at the route (dependency) and re-checked in services for sensitive
  ops (multi-layer, per platform convention).
- Rate limits per group; expensive endpoints (`/compares:data`, full-scan runs)
  return a cost estimate and require `confirm=true` unless the group opts into
  auto-confirm.
- No secret material is ever returned by any endpoint.
- Every mutation writes an `audit_log` entry with `trace_id`.

## 4. OpenAPI & SDKs

- OpenAPI 3.1 auto-published at `/api/v1/openapi.json`; Swagger UI in non-prod.
- Generated typed clients (Python, TypeScript) for the Web UI and for customers.
- The MCP tool schemas ([06](./06-mcp.md)) are generated from the same Pydantic
  models, so API and MCP never drift.
