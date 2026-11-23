# 02 — Multi-Tenancy & Isolation

> Requirement: *"Make it multi-tenant so multiple groups in an organization can
> work. Each group should be completely independent from others."*

## 1. Tenancy model

Two levels:

```
Organization (tenant)          e.g. "Acme Corp"
   └── Group (workspace)        e.g. "Payments-DQ", "Risk-Analytics"
         ├── Members (users with roles)
         ├── Connections (Snowflake, Oracle) + secrets
         ├── Monitors → DQ rules → runs → incidents
         └── Compare jobs → results
```

- **Organization** is the customer/billing boundary.
- **Group** is the *ownership and isolation unit*. Every domain object carries a
  `group_id`. "Completely independent" is enforced here: a user acting in group A
  cannot list, read, mutate, or run anything in group B, and group A's queries
  physically cannot touch group B's connections, warehouses, or secrets.
- A user may belong to multiple groups (with a role per group), but **every
  request operates in exactly one active group context**.

## 2. Isolation guarantees (the contract)

| Dimension | Guarantee | Mechanism |
|-----------|-----------|-----------|
| **Metadata** | A group's rows are invisible to other groups. | Postgres Row-Level Security keyed on `group_id` (see 3.1). |
| **Credentials** | A group's Snowflake/Oracle secrets are unreadable by any other group. | Secrets stored under per-group paths in Vault/AWS SM; access policy checks group context. |
| **Compute** | A group's compare/observability jobs cannot read another group's data or exhaust another group's resources. | Per-group Spark job identity + per-group concurrency & warehouse quotas. |
| **Results / artifacts** | Bad-record samples, exports, cell diffs. | S3 prefix per group + IAM/bucket policy; presigned URLs scoped to group. |
| **Audit** | Every access is attributable and scoped. | `audit_log` records `org_id`, `group_id`, `user_id`, action. |

## 3. Isolation mechanisms

### 3.1 Metadata isolation (D1)

**Default: shared Postgres, `group_id` on every table, enforced by RLS.**

- Every tenant-scoped table has `org_id` + `group_id` NOT NULL.
- A Postgres RLS policy on each table restricts rows to the `group_id`(s) in the
  session's `app.current_group` GUC, which the API sets per transaction:
  ```sql
  SET LOCAL app.current_org   = :org_id;
  SET LOCAL app.current_group = :group_id;
  ```
  ```sql
  CREATE POLICY grp_isolation ON monitors
    USING (group_id = current_setting('app.current_group')::uuid);
  ```
- Defense in depth: the ORM layer *also* injects `group_id` filters (belt and
  suspenders); RLS is the backstop that catches any missed filter.

**High-isolation tier (optional):** for customers who contractually require
physical separation, the same schema can be deployed **schema-per-group** (or
database-per-org). The data-access layer is written against a `TenantContext` so
switching from RLS to schema-routing is a config change, not a rewrite.

> Why RLS as default, not schema-per-group everywhere: thousands of groups ×
> dozens of tables = migration and connection-pool explosion. RLS gives strong,
> auditable isolation with one migration path. Physical separation is reserved
> for the tier that pays for it.

### 3.2 Credential isolation

- Secrets live in the `SecretStore` (Vault / cloud secrets manager / local
  keystore, per runtime mode) at `dvp/{org_id}/{group_id}/connections/{conn_id}`.
- The `SecretStore.get(conn_id, ctx)` call asserts `ctx.group_id` matches the
  secret's path before returning — a group context can only resolve its own
  secrets.
- Private keys (key-pair auth) and OAuth refresh tokens are **write-once /
  read-by-engine-only**; never returned to the UI or API responses.

### 3.3 Compute isolation

- Each Spark compare job is launched into a K8s namespace/service-account bound
  to the group; credentials injected at launch, torn down at completion.
- **Per-group quotas**: max concurrent runs, max Spark executors, and a mapping
  to the group's own Snowflake warehouse (so their compute spend is theirs).
- Observability SQL always runs on the *group's own* Snowflake warehouse using
  the group's connection — we never co-mingle warehouses.

## 4. Request lifecycle & tenant context

```
1. Request arrives with a platform JWT (from platform SSO — see 03).
2. AuthN: verify JWT signature/expiry → user_id, org_id, groups[].
3. Group selection: X-Group-Id header (or path segment) → active group.
4. AuthZ: assert user is a member of that group; load role & permissions.
5. Build TenantContext(org_id, group_id, user_id, role, permissions).
6. Open DB transaction → SET LOCAL app.current_org/current_group (RLS).
7. Handler runs; all repositories require TenantContext; no ambient/global access.
8. Response; audit_log entry written; transaction commits.
```

`TenantContext` is a required parameter (via FastAPI dependency) for every
service and repository method. There is no code path that reads tenant data
without it — this is enforced by making the repository base class constructor
demand a context.

## 5. RBAC

Roles per group:

| Role | Capabilities |
|------|--------------|
| **Viewer** | Read monitors, analyses, rules, run results, compare results. No mutations, no runs. |
| **Editor** | Viewer + CRUD monitors/rules, trigger runs & compares, auto-generate rules. |
| **Group Admin** | Editor + manage members, connections, secrets, schedules, quotas for the group. |
| **Org Admin** | Create/delete groups, set org policy, manage org membership, view usage/billing. **No data read** into a group unless also a member (must be explicit + audited). |

- Permissions are checked at the API boundary (route dependency) *and* re-checked
  in the service layer for sensitive operations (mirrors the platform's
  multi-layered permission convention).
- Org Admin "break-glass" access into a group is possible but requires an explicit
  action, is time-boxed, and is loudly audited.

## 6. Noisy-neighbor & fairness

- Per-group concurrency limits in the orchestrator (a group can't queue 10k jobs
  and block others).
- Per-group rate limits at the gateway.
- Spark jobs are cost-attributed per group (labels → billing/showback).

## 7. What could break isolation, and how we prevent it

| Risk | Mitigation |
|------|------------|
| A query forgets its `group_id` filter | RLS makes the DB return zero rows regardless — the filter is not the only guard. |
| A secret is fetched with the wrong context | `SecretStore` path assertion rejects cross-group reads. |
| A Spark job reads the wrong warehouse | Credentials are per-job, per-group; there is no shared/global connection. |
| Cache key collision across groups | All cache keys are prefixed with `{org_id}:{group_id}:`. |
| MCP agent escapes its scope | MCP session carries the user's `TenantContext`; every tool re-derives scope from it (see [06](./06-mcp.md)). |
| Test data leaks between test groups | E2E tests create isolated ephemeral groups and assert cross-group access returns 404/empty. See [09](./09-testing-strategy.md#4-multi-tenancy-tests). |

## 8. Local-mode caveat

The strong isolation guarantees above (RLS, per-group secret paths, per-group
compute quotas) are a **server / prod** feature. In **desktop / local mode**
([01 §5.1](./01-architecture.md#51-desktop--local-mode-in-memory-zero-external-deps))
the metadata store is in-memory SQLite, which has **no Row-Level Security**:

- Isolation there relies solely on the always-present application-layer `group_id`
  filtering (the same filters that run in every mode) — but **without the RLS
  backstop**.
- Local mode is intended for a single user / single group (the common desktop
  case). One org + one group is auto-provisioned on first `dvp run`.
- Multi-group logic still functions locally for development, but we do **not**
  claim hard multi-tenant isolation on SQLite. Anyone needing enforced isolation
  runs in server/prod mode on Postgres, where RLS is active.
- The multi-tenancy E2E isolation suite ([09 §4](./09-testing-strategy.md#4-multi-tenancy-tests))
  therefore runs against **Postgres**, not the local SQLite backend.

## 9. Platform authentication

Distinct from *data-source* auth (Snowflake/Oracle, see [03](./03-snowflake-connectivity.md)),
this is how users log in to the platform itself. The target is **SaaS: one
deployment, many customer orgs** (D8), so platform auth is multi-tenant SSO.

### 9.1 SaaS / server (target)
- **Enterprise SSO via OIDC** is the primary mechanism. Each customer org
  federates to **its own IdP** (Okta, Entra/Azure AD, Ping, etc.); SAML-only IdPs
  are bridged. *(Specific IdP details are pending from the customer — D2.)*
- The IdP issues identity; the platform mints its own short-lived JWT carrying
  `user_id`, `org_id`, and group memberships, which drives the request lifecycle
  in §4 and RBAC in §5.
- Org → IdP mapping is per org, so User A at Org 1 and User B at Org 2 authenticate
  against entirely separate identity providers in the same deployment.
- SCIM (or IdP group claims) can drive group membership provisioning later.

### 9.2 Desktop / local (current bring-up)
- Per your direction, **desktop mode runs with no password**: a single local user
  is auto-provisioned into one org + one group on first `dvp run`. No IdP, no login
  screen — appropriate for a single engineer on their own machine.
- This is a convenience for local development and single-user desktop use; it does
  **not** apply to the SaaS/server tiers, which always require SSO.
- The auth layer is an abstraction (`IdentityProvider`): `local` → the no-password
  provider; `server`/`prod` → the OIDC provider. Same downstream `TenantContext`,
  so RBAC/isolation code is identical regardless of how the user authenticated.
