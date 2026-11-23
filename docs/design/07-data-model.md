# 07 — Data Model (Metadata Store)

PostgreSQL 16. Every tenant-scoped table carries `org_id` + `group_id` with
Row-Level Security (see [02](./02-multi-tenancy.md)). Alembic migrations; never
edit a shipped migration (idempotent, additive only).

## 1. ERD (logical)

```
organization 1───* group 1───* membership *───1 user
                    │
                    ├──* connection ──(secret_ref)──▶ SecretStore (external)
                    │
                    ├──* monitor 1───* monitor_table 1───* dq_rule
                    │        │                   │
                    │        │                   └──* watermark
                    │        │
                    │        1───* monitor_run 1───* rule_result
                    │                    │       1───* metric
                    │                    └───────1───* incident
                    │
                    └──* compare_job 1───1 compare_result ──▶ S3 (detail artifact)

audit_log (append-only, references any entity)
```

## 2. Core tables

### Tenancy & identity
```
organization(id, name, plan, created_at)
group(id, org_id→organization, name, description, isolation_tier, quotas jsonb, created_at)
user(id, email, external_idp_sub, display_name, created_at)
membership(id, group_id→group, user_id→user, role, created_at)     -- role: viewer|editor|group_admin|org_admin
```

### Connections
```
connection(
  id, org_id, group_id→group,
  source_type,                      -- snowflake | oracle | file
  name, coordinates jsonb,          -- account/host/db/warehouse/role/...
  auth_method,                      -- key_pair | oauth_sso | external_browser | password | wallet
  secret_ref,                       -- pointer into SecretStore; NO secret material here
  allowed_scopes jsonb,             -- databases/schemas this group may touch
  status, created_by, created_at, updated_at
)
```
> Secrets (private keys, OAuth refresh tokens, passwords) are **never** stored in
> Postgres — only `secret_ref`. See [03](./03-snowflake-connectivity.md).

### Monitors & rules
```
monitor(
  id, org_id, group_id, connection_id→connection,
  name, description, tags text[],
  scope jsonb,                      -- { database, schema, tables[] | pattern }
  schedule_cron, timezone,
  status,                           -- active | paused | archived
  created_by, created_at, updated_at
)

monitor_table(
  id, monitor_id→monitor,
  database, schema, table_name,
  detected_key jsonb,               -- from key detection
  profile_snapshot jsonb,           -- last full profile (for autogen & drift)
  created_at
)

dq_rule(
  id, org_id, group_id, monitor_id→monitor, monitor_table_id→monitor_table,
  name, description,
  type,                             -- not_null | unique | range | in_set | regex | freshness | custom_sql | ...
  columns text[],
  config jsonb,                     -- thresholds, operators, allowed_values, tolerance, regex...
  severity,                         -- info | warning | critical
  scope,                            -- incremental | full
  state,                            -- suggested | enabled | disabled
  confidence numeric,               -- for auto-generated rules
  version int,                      -- bumped on edit (runtime edit safety)
  is_deleted bool,                  -- soft delete
  created_by, updated_by, created_at, updated_at
)

watermark(
  id, monitor_table_id→monitor_table,
  strategy,                         -- stream | changes | high_water_mark | manual | full
  column_name,                      -- for HWM
  last_value jsonb,                 -- last processed value (typed)
  stream_name,                      -- for Snowflake stream strategy
  updated_at
)
```

### Runs, results, metrics, incidents
```
monitor_run(
  id, org_id, group_id, monitor_id→monitor, monitor_table_id→monitor_table,
  status,                           -- queued | running | succeeded | failed | partial
  ruleset_snapshot jsonb,           -- exact rule versions evaluated (runtime-edit safety)
  partition_predicate text,         -- the delta predicate used
  rows_analyzed bigint,
  started_at, finished_at, error
)

rule_result(
  id, monitor_run_id→monitor_run, dq_rule_id→dq_rule, rule_version int,
  passed bool, metric_value numeric, threshold numeric,
  observed jsonb,                   -- extra detail
  bad_sample_ref                    -- S3 pointer to sample offenders
)

metric(                             -- time series for charts
  id, org_id, group_id, monitor_table_id→monitor_table,
  ts, name,                         -- null_rate.email, row_count, freshness_hours, distinct_count.status ...
  value numeric, tags jsonb
)   -- consider partitioning by month; candidate for a timescale/rollup table

incident(
  id, org_id, group_id, monitor_id, monitor_table_id, dq_rule_id,
  severity, status,                 -- open | acknowledged | resolved
  first_seen, last_seen, resolved_at,
  summary, sample_ref
)
```

### Data testing
```
compare_job(
  id, org_id, group_id,
  kind,                             -- count | schema | data
  source jsonb, target jsonb,       -- { connection_id, db, schema, table, filter }
  options jsonb,                    -- key, columns, mapping, tolerance, flags...
  status,                           -- queued|running|succeeded|failed|partial
  created_by, created_at, finished_at, error
)

compare_result(
  id, compare_job_id→compare_job,
  summary jsonb,                    -- counts, match_rate, per_column_mismatch...
  detected_key jsonb,
  warnings jsonb,
  detail_ref,                       -- ResultStore pointer: object-store URI OR Snowflake table ref (D9)
  page_index_ref                    -- for paged UI retrieval
)
```

### Catalog (table search)
```
catalog_table(
  id, org_id, group_id, connection_id,
  database, schema, table_name,
  row_count, bytes, last_altered, column_meta jsonb,
  is_monitored bool, tsv tsvector,  -- full-text search
  refreshed_at
)
```

### Audit
```
audit_log(                          -- append-only
  id, org_id, group_id, user_id,
  action,                           -- rule.update, compare.run, connection.create, breakglass.access...
  entity_type, entity_id,
  detail jsonb, ip, ts
)
```

## 3. Key modeling decisions

- **JSONB for `config`, `scope`, `options`, `summary`** — rule types and options
  evolve fast; JSONB avoids a migration per new rule type while still being
  queryable. Strongly-typed at the app boundary via Pydantic.
- **`ruleset_snapshot` on `monitor_run`** — makes runtime rule editing safe and
  results reproducible: you can always see exactly which rule versions produced a
  historical result ([04 §3.5](./04-observability.md#35-runtime-rule-editing-1f)).
- **Soft-delete rules** (`is_deleted`) so historical `rule_result`s remain
  interpretable.
- **`metric` as a time series** feeds all charts; partition by month and roll up
  old data to keep it lean. (Open: evaluate TimescaleDB vs native partitioning.)
- **Heavy detail off-Postgres via `ResultStore`** — cell-level diffs and
  bad-record samples (which contain real data values) are never stored in the
  metadata DB. They are written through the configurable `ResultStore` and
  referenced by `detail_ref` / `bad_sample_ref`, which is an **opaque pointer**:
  an object-store URI by default, or a **table reference in the customer's own
  Snowflake** when Snowflake-native storage is configured for the group (D9, see
  [05 §8.1](./05-data-testing.md#81-resultstore-backends-d9--data-residency)).
  Postgres stays small and fast regardless of result volume.
- **RLS on every `group_id` table** + app-layer filtering (defense in depth).
  (RLS is a Postgres/server feature; local SQLite mode relies on app-layer
  filtering only — see [02 §8](./02-multi-tenancy.md#8-local-mode-caveat).)

## 4. Retention & lifecycle

- Runs/metrics/results retained per group policy (default 13 months of metrics for
  trend charts; raw run detail 90 days), then rolled up/archived to S3.
- Incidents retained indefinitely (compliance) unless the group purges.
- Audit log append-only, exported to immutable storage for compliance.
