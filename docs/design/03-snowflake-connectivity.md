# 03 — Snowflake Connectivity

> Requirements: *"connect to Snowflake using key-pair and also SSO. When using
> SSO, make sure we don't open a new webpage for every request."*

## 1. Connector abstraction

All sources sit behind a `Connector` protocol so the observability and testing
engines don't care whether they're talking to Snowflake, Oracle, or a file.

```python
class Connector(Protocol):
    def test(self) -> HealthResult: ...
    def get_schema(self, table: TableRef) -> list[ColumnDef]: ...
    def count(self, table: TableRef, predicate: str | None) -> int: ...
    def execute(self, sql: str) -> QueryResult: ...          # pushdown aggregates
    def spark_reader(self, spark, table: TableRef, partition: PartitionSpec): ...
```

Snowflake gets a `SnowflakeConnector` with an `Authenticator` strategy. Auth is
the interesting part.

## 2. Connection object & secret split

A **Connection** record (Postgres) holds non-secret coordinates:

```
connection
├── id, group_id, org_id
├── source_type            = 'snowflake'
├── account, region, host
├── default_warehouse, default_role, default_database
├── auth_method            = 'key_pair' | 'oauth_sso' | 'external_browser'
└── secret_ref             → pointer into SecretStore (never the secret itself)
```

The **secret** (private key, OAuth client secret + refresh token, etc.) lives in
Vault/AWS SM at `dvp/{org_id}/{group_id}/connections/{id}` (see [02](./02-multi-tenancy.md#32-credential-isolation)).

## 3. Key-pair authentication

The straightforward, service-account path — used for scheduled monitor runs and
compares where no human is present.

- Admin uploads (or we generate) an RSA key pair. The **private key** (optionally
  encrypted with a passphrase) is stored in the SecretStore; the **public key**
  is registered on the Snowflake user (`ALTER USER … SET RSA_PUBLIC_KEY=…`).
- At connect time the connector loads the private key from the SecretStore and
  passes it to the driver:

```python
ctx = snowflake.connector.connect(
    account=conn.account,
    user=conn.user,
    private_key=load_der_private_key(secret.private_key, secret.passphrase),
    warehouse=conn.default_warehouse,
    role=conn.default_role,
    session_parameters={"QUERY_TAG": f"dvp:{group_id}:{job_id}"},
)
```

- **Key rotation** is first-class: we support two active public keys on the
  Snowflake user (`RSA_PUBLIC_KEY` + `RSA_PUBLIC_KEY_2`) so keys can be rotated
  with zero downtime. Rotation is an admin action, audited.
- Recommended default for **automated** workloads (scheduled runs, compares)
  because it needs no human and no browser.

## 4. SSO without a browser per request

This is the requirement that drives the design. Snowflake offers three SSO-ish
mechanisms; only one is right for a server that runs many queries.

### 4.1 The three options and why we pick OAuth

| Mechanism | Browser behavior | Good for | Verdict |
|-----------|------------------|----------|---------|
| `authenticator=externalbrowser` (naive) | **Opens a browser on every `connect()`** | Nothing at scale | ❌ This is exactly what the requirement forbids. |
| `externalbrowser` **+ ID-token caching** (`client_store_temporary_credential=True`) | Browser once; subsequent connects reuse a cached ID token until it expires | Desktop / interactive CLI use | ⚠️ Fallback only — token cache is local to a host, not suitable for a horizontally-scaled server. |
| **Snowflake OAuth** (External OAuth via the org IdP, or Snowflake OAuth) — authorization-code flow, then **refresh tokens** | Browser **once** during connection setup; server refreshes access tokens silently forever after | Multi-user server, scheduled + interactive | ✅ **Primary design.** |

**Decision (D3): Snowflake OAuth as the primary SSO path.**

### 4.2 OAuth flow (one-time browser, then silent)

```
   ── One-time authorization (happens ONCE when a user links their SSO ──
   1. User clicks "Connect via SSO" in the UI for a connection.
   2. We redirect to the IdP / Snowflake OAuth authorize endpoint
      (scope includes `refresh_token`, `session:role-any`).
   3. User authenticates in the browser (this is the ONLY browser popup).
   4. Callback returns an authorization code.
   5. We exchange code → { access_token (short-lived), refresh_token (long-lived) }.
   6. refresh_token is encrypted and stored in the SecretStore, keyed by
      (group_id, connection_id, user_id).

   ── Every subsequent query (NO browser) ──
   7. Connector needs a token → TokenManager.get_access_token(ctx):
        • if cached access_token still valid  → use it
        • else                                → POST refresh_token to token endpoint,
                                                get a new access_token, cache it
   8. connect(authenticator='oauth', token=access_token, ...)
```

- **No browser is ever opened after step 3.** Access tokens are refreshed
  server-side using the stored refresh token.
- Access tokens are cached in Redis with TTL = token expiry minus a safety margin,
  keyed `{org}:{group}:{conn}:{user}:sf_access_token`. A refresh is triggered
  proactively before expiry (single-flight lock to avoid a refresh stampede).
- Refresh-token expiry / revocation → the *next* interactive session re-links
  (browser once again); scheduled jobs that rely on a user token surface a clear
  "re-authentication required" incident rather than silently failing (golden
  rule #3).

### 4.3 Two SSO sub-modes

1. **User-delegated SSO (interactive):** queries run *as the logged-in user*
   using their OAuth token. Best for ad-hoc analysis; respects the user's
   Snowflake roles/grants. Token bound to `user_id`.
2. **Service SSO (External OAuth client-credentials):** for scheduled monitor
   runs, a group-level External OAuth integration issues tokens for a service
   principal — no user, no browser, ever. This is preferred for automation when
   the org mandates SSO (can't use key-pair).

> Guidance we'll bake into the UI: **use key-pair or service External-OAuth for
> scheduled monitors; use user-delegated OAuth SSO for interactive exploration.**

### 4.4 `externalbrowser` + cached token (fallback)

For local desktop / dev usage we support `authenticator=externalbrowser` with
`client_store_temporary_credential=True` (and `client_request_mfa_token=True` for
MFA caching). This caches the ID token on the local host so repeated connects in
a dev session don't re-prompt. **Not** used in the multi-instance server tier.

## 5. Connection pooling & session reuse

Opening a Snowflake session per query is slow and wasteful. We pool.

- A **per-(group, connection, role, warehouse) pool** of live sessions, with idle
  eviction and a max size (part of the group's quota). Keys include role +
  warehouse because those change the session semantics.
- Sessions use `CLIENT_SESSION_KEEP_ALIVE=True` so long-lived pooled sessions
  aren't dropped mid-idle.
- For OAuth sessions, the pool validates the token before handing out a session
  and transparently refreshes/reconnects if the access token has rotated.
- Pools are **strictly per-group** — a session is never shared across groups
  (isolation, [02](./02-multi-tenancy.md)).
- Pool metrics (active/idle/wait) exported to Prometheus per group.

## 6. Oracle connectivity (for data testing)

- `oracledb` thin mode for metadata/count/schema ops (no Oracle Instant Client
  needed in the image).
- For Spark data compare, JDBC via the ojdbc driver with **partitioned reads**
  (`partitionColumn`, `lowerBound`, `upperBound`, `numPartitions`) so large tables
  parallelize. Auth via wallet or user/password stored in the SecretStore.

## 7. Failure handling & health

- `Connection.test()` validates auth + a trivial `SELECT 1` and reports a
  structured `HealthResult` (never a bare boolean — we distinguish "auth failed"
  from "network" from "warehouse suspended").
- Auth failures during scheduled runs raise a typed `AuthExpiredError` that
  becomes a visible incident + warning on the run, not a silent empty result.
- All Snowflake queries carry a `QUERY_TAG` of `dvp:{group}:{monitor|compare}:{run}`
  so customers can trace our spend in their `QUERY_HISTORY`.

## 8. Security notes

- Private keys and refresh tokens: encrypted at rest (SecretStore + KMS), never
  logged, never in API responses, redacted in traces.
- TLS enforced to Snowflake (OCSP checks on).
- Least-privilege roles recommended in setup docs; we warn if a connection uses
  `ACCOUNTADMIN`.
- Per-connection allow-listing of databases/schemas a group may monitor, so a
  connection's blast radius is bounded.
