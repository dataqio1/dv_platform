# 06 — MCP Server

> Requirement: *"make sure this software has MCP so users can connect with their
> agents."*

The platform ships a first-class **Model Context Protocol** server so a user's AI
agent (Claude, etc.) can list monitors, read analyses, author/inspect DQ rules,
run compares, and read results — all scoped to that user's group and permissions.

## 1. Design principles

1. **The MCP server is a thin adapter over the same service layer the REST API
   uses.** No business logic is duplicated; MCP tools call the exact same
   application services, so agents and humans get identical behavior and identical
   isolation.
2. **Tenant scope is derived from the authenticated session, never from tool
   arguments.** An agent cannot pass a `group_id` to escape its scope — the
   `TenantContext` comes from the MCP session's auth, and every tool re-derives
   scope from it (see [02](./02-multi-tenancy.md#7-what-could-break-isolation)).
3. **Read-first, write-guarded.** Read tools are freely available; mutating tools
   (create monitor, delete rule, run compare) respect the caller's RBAC role and
   are audited exactly like API calls.
4. **Resources for browsing, tools for actions.** Monitors/tables/results are
   exposed as MCP *resources* (URI-addressable, cacheable); operations are *tools*.

## 2. Transport & auth

- **Transport**: streamable HTTP MCP (works for remote agents) and stdio (for
  local/desktop). Built on the official Python MCP SDK.
- **Auth**: OAuth 2.1 bearer (the platform's own tokens). The MCP session
  exchanges the token for a `TenantContext(org, group, user, role)`. Group
  selection is either bound at token issuance or chosen via a `select_group` tool
  when the user belongs to several.
- Deployed as a sidecar to the API service (shares the service layer in-process)
  or standalone calling the API over the network — configurable.

## 3. Resources

| Resource URI | Description |
|--------------|-------------|
| `dvp://monitors` | List of the group's monitors (health, status). |
| `dvp://monitors/{id}` | A monitor with its rules, schedule, latest run. |
| `dvp://monitors/{id}/runs/{run_id}` | A run's metrics, rule results, incidents. |
| `dvp://tables/{conn}/{db}/{schema}/{table}` | Deep analysis/profile for a table. |
| `dvp://incidents` | Open incidents across the group. |
| `dvp://compares/{job_id}` | A compare job's summary + paged differences. |

## 4. Tools

### Observability
| Tool | Args | Returns |
|------|------|---------|
| `search_tables` | `query, connection_id?` | matching tables + monitored flag |
| `get_table_analysis` | `connection_id, table` | profile, freshness, volume, schema history |
| `list_monitors` | `status?, tag?` | monitors |
| `get_monitor` | `monitor_id` | monitor + rules + latest run |
| `create_monitor` | `connection_id, scope, schedule` | new monitor *(RBAC: Editor)* |
| `list_rules` / `get_rule` | `monitor_id` / `rule_id` | rules |
| `upsert_rule` | `monitor_id, rule spec` | created/updated rule *(Editor)* |
| `autogenerate_rules` | `monitor_id, level:1|2|3` | suggested rules *(Editor)* |
| `run_monitor` | `monitor_id` | run id (async) *(Editor)* |
| `get_run` | `run_id` | metrics, rule results, incidents |
| `list_incidents` / `resolve_incident` | filters / `incident_id` | incidents *(resolve: Editor)* |

### Data testing
| Tool | Args | Returns |
|------|------|---------|
| `compare_counts` | `source, target, filter?` | count result |
| `compare_schema` | `source, target` | schema diff |
| `detect_compare_key` | `source, target` | proposed key + confidence |
| `compare_data` | `source, target, options` | compare job id (async) *(Editor)* |
| `get_compare_result` | `job_id, page?, filter?` | summary + cell-level diffs |

### Session
| Tool | Args | Returns |
|------|------|---------|
| `select_group` | `group_id` | confirms active group (must be a member) |
| `whoami` | – | user, active group, role, permissions |

## 5. Async operations

Runs and data compares are asynchronous. MCP tools return a job id immediately;
the agent polls `get_run` / `get_compare_result`, or subscribes to progress via
MCP notifications/streaming. Long compares stream status updates so the agent can
report progress to the user.

## 6. Safety & guardrails

- Every mutating tool is subject to the same RBAC + audit as the REST API.
- Tools that could be expensive (e.g. `compare_data` on huge tables, `run_monitor`
  full-scan) return a **cost/row estimate first** and require an explicit
  `confirm: true` (or respect a per-group auto-confirm policy) — agents shouldn't
  silently spend a customer's warehouse credits.
- Rate-limited per group; results paginated to keep token usage sane for agents.
- All tool inputs validated with Pydantic; structured errors returned so agents
  can self-correct.

## 7. Why this matters

Because the MCP server is a thin skin over the service layer, the isolation,
RBAC, auditing, and warning-propagation guarantees from the rest of the platform
hold automatically for agents — there is no second, weaker code path. Agents are
first-class *clients*, not a bolt-on.
