# Data Observability UI — Backend Work Context (handoff)

> Saved mid-task on request. Goal: implement whatever backend is needed to support the
> **data_observability** Figma UI.
> Figma: https://www.figma.com/design/C4XUpOza6ApxQ4W3KuJgWY/data_observability (file key `C4XUpOza6ApxQ4W3KuJgWY`, page `0:1`)
> Figma MCP session id used: `b0b2193b-5db9-4aad-b52d-8b1c93a76a1e`

## Repo facts
- Python 3.12, FastAPI backend under `src/dvp`, API prefix `/api/v1`, routers in `src/dvp/api/routers/`.
- NOT a git repo. Alembic migrations in `migrations/`. Frontend (Vite/React/TS) in `frontend/`.
- Pydantic schemas in `src/dvp/api/schemas.py`. DB models in `src/dvp/db/models.py`.
- Observability domain logic in `src/dvp/observability/` (service, runner, incidents, rules,
  anomaly, baseline, watermark, pillars, alertpolicy, lineage, scheduler, autogen, etc.).
- `ApiResponse` wrapper in `src/dvp/core/api_response.py` (with `Warning_` support).

## The design — 18 top-level frames (node ids)
Full textual analysis already done (see below). Frame ids for screenshotting:
`3:12627`, `16:3060`, `3:13792`, `3:13179`, `16:2657`, `16:3302`, `51:10138`, `51:10416`,
`51:10654`, `51:10929`, `51:11188`, `19:3838`, `19:4211`, `19:4686`, `37:6068`, `37:5680`,
`37:5474`, `37:5275`.
Extracted XML cached at `/tmp/figma.xml` (parser scripts `/tmp/parse.py`, `/tmp/content2.py`).
Raw metadata: `/Users/dhiraj/.claude/projects/-Users-dhiraj-Documents-dataq-dv-platform/e5542a0f-228a-46a0-8e54-95f85317530e/tool-results/mcp-figma-get_metadata-1792508153339.txt`

### Product model from the design
5 pillars: **Freshness, Volume, Schema, Distribution, Data Quality** (key integrity + custom SQL).
Two source modes: **file datasets (Amazon S3)** and **tables (Snowflake)**.

### Screens → what they display
1. `3:12627` **Monitors list/dashboard** — 4 KPI health cards, status filter chips (5), search+filters,
   data table (9 cols, owner, status), pagination.
2. `16:3060` **Monitor details** — "Amazon S3 · commerce_v1 · 3 file datasets · 16 checks"; volume
   history chart (actual vs expected band, incident markers); monitor health (passing checks, coverage
   bar, five-pillar health); open-incident banner; monitored assets table; custom SQL + notifications panels.
3. `3:13792` **Incident investigation** — silence banner (actor/until/ticket), incident context
   (severity/property/affected asset/related monitor/schema state/silence state), evidence (volume
   analysis chart, anomaly metrics, dataset/eval evidence), investigation timeline (events), audit entry
   (EVT/req/actor/action/scope/outcome), comment box, recommended actions/checklist.
4. `3:13179` **Create monitor · Select data (files/S3)** — 3-step wizard; source type; file dataset
   selection table (verified schema / blocked), bulk select, pagination, selection total.
5. `16:2657` **Create monitor · SQL & notifications (files)** — custom SQL rule editor (name, dataset
   scope, saved schema, connection, query, validate/test, pass/warn/error on failed_rows); execution +
   notification prefs (per-pillar, events, Slack/Email channels, timing).
6. `16:3302` **Create monitor · Review (files)** — summary cards (datasets/files/checks), dataset preview
   table (prefix pattern, matched files, schema, arrival window, notifications), included SQL rule,
   severity-thresholds table (warning/error per pillar), execution review.
7. `51:10138` **Create monitor · Configure checks (files, all pillars workbench)**.
8-11. Pillar editors: `51:10416` Freshness, `51:10654` Volume, `51:10929` Schema, `51:11188`
   Distribution — each with policy/thresholds, simulate/preview, validate&apply.
12. `19:3838` **Create monitor · Select data & table behavior (tables/Snowflake)** — connection, saved
   schema; **table loading-behavior detection** (Truncate&load / SCD1 / SCD2 / Append-only / Other) with
   confidence %, accept/manual-override, column mappings (LOADED_AT, BATCH_ID, BUSINESS_KEY, VALID_FROM,
   IS_CURRENT, INGESTED_AT, EVENT_ID), focused table editor (confirm type + mappings).
13. `19:4211` **Create monitor · Configure checks (tables)** — behavior groups, coverage counts,
   coverage&overrides table, five-pillar config, custom SQL editor, execution+notifications.
14. `19:4686` **Create monitor · Review (tables)** — summary, type groups, pillar breakdown, coverage
   table, severity thresholds, execution review.
15. `37:6068` **Admin · Audit log** — filters (time/entity/actor/trace), export, events table, event
   detail drawer (identity/state/actor/entity+scope/retained incident state/note/before-after/trace ids).
16. `37:5680` **Admin · People & access** — membership overview, members table, service identities,
   role permission matrix.
17. `37:5474` **Admin · Organization settings** — org identity, auth/session policies, service
   accounts & API keys, governance policies (alert silence policy, deletion & audit policy).
18. `37:5275` **Silence alert · Add notes** — silence scope (incident vs broader), silence window
   (duration/start/expiry), channel impact, confirm.

## Backend API surface already present (route inventory)
### observability.py (read in full — it's comprehensive)
- watermark: `POST /connections/{id}/watermark:detect`, `:validate`, `POST /monitors/{id}:watermark`
- tables/pillars: `GET /monitors/{id}/tables`, `POST .../tables/{ref}/snapshot`, `POST .../tables/{ref}:profile`,
  `GET .../tables/{ref}/pillars`, `GET .../tables/{ref}/pillars/{pillar}/history`
- incidents: `GET /incidents`, `GET /monitors/{id}/incidents`, `GET .../tables/{ref}/incidents`,
  `POST /incidents/{id}:ack|:resolve|:reopen`, `GET/POST /incidents/{id}/comments`,
  `POST /observability/points/{id}:exclude`
- run/eval: `POST /monitors/{id}:run`, `POST /monitors/{id}/rules/{rid}:evaluate`
- rules/metrics: `POST /monitors/{id}/rules/{rid}:record`, `GET .../rules/{rid}/history`,
  `GET .../tables/{ref}/rules`, `GET .../tables/{ref}/columns`, `GET .../tables/{ref}/columns/{col}/history`
### monitors.py
- `POST ""`, `POST /{id}:load-type`, `PATCH /{id}/assignment`, `GET ""`, `GET /{id}`, `PATCH /{id}`,
  `DELETE /{id}`, `POST /{id}:status`, `GET/POST /{id}/rules`, `PATCH/DELETE /{id}/rules/{rid}`,
  `POST /{id}/rules:autogenerate`
### projects.py
- `POST ""`, `GET ""`, `GET /{id}`, `PATCH /{id}`, `GET /{id}/test-cases`, `DELETE /{id}`, `POST /{id}:restore`
### Routers registered in app.py (all under /api/v1)
health, lineage, audit, me, members, system, integrations, metrics, connections, dbt, monitors,
observability, compares, projects, tags, table_metadata, reports, settings.

## ✅ RESOLVED (2026-10-20 session) — gap-filling endpoints shipped
Gap analysis done against the 18 screens; the 134-endpoint surface already covered most of it.
Implemented the remaining concrete gaps (all with end-to-end tests, no mocks):
- **Incident silence** (screens 3, 18): `POST /incidents/{id}:silence` (scope incident|monitor,
  `until` or `duration_hours`, note, ticket_url) + `:unsilence`. Incident dict now exposes
  `silenced`/`silenced_until`/`silenced_by`/`silence_scope`/`silence_note`/`ticket_url`. A live
  **monitor-scoped** silence suppresses new-incident notifications for that monitor.
- **Monitor health KPIs** (screen 1): `GET /monitors:summary` → `total`, per-monitor `health`
  {healthy|warning|error|paused}, `by_status`, active/silenced `incidents`, `coverage_pct`.
- **Role permission matrix** (screen 16): `GET /access/roles` (static, derived from
  core.tenancy `_ROLE_PERMISSIONS`).
- **Service accounts / API keys** (screens 16, 17): `GET/POST /access/service-accounts`,
  `:rotate`, `DELETE`. Keys stored as sha256 + prefix; plaintext returned once.
- **Org settings / governance** (screen 17): `GET/PUT /settings/{organization,auth,governance}`
  (namespaced JSON blobs via new `OrgSetting`).
- **Audit log** (screen 15): `GET /audit` gained `action`/`user_id`/`since`/`until` filters;
  new `GET /audit/export` returns CSV.
New files: `services/access.py`, `services/org_settings.py`, `api/routers/access.py`,
`tests/test_observability_ui_backend.py`. New models: `OrgSetting`, `ServiceAccount` + incident
silence columns. Migration `0020_observability_ui_backend` (idempotent, applies on fresh + existing DB).
3 pre-existing failures remain in evaluator/scheduler/reports (quality-pillar `configured`
logic in pillars.py/evaluator.py — untouched by this session, predate it). Everything new is green + ruff-clean.

## WHAT WAS LEFT TO DO (original resume notes — now addressed above)
1. **Finish the route inventory** for: members, audit, integrations (notifications/Slack/Email/channels),
   settings (org settings, auth/session, service accounts, API keys, governance), connections,
   table_metadata, me. (Commands above; grep got truncated — rerun per-file.)
2. **Gap analysis** — map each of the 18 screens to existing endpoints; list missing endpoints/fields.
   Known likely gaps to confirm:
   - **Table loading-behavior DETECTION** (screen 12): SCD1/SCD2/Append/Truncate classification with
     confidence + suggested column mappings. `POST /{id}:load-type` exists (set), but is there a *detect*
     endpoint? Check `observability/autogen.py` / watermark detection.
   - **Monitor health summary KPIs** for the list dashboard (screen 1) — aggregate counts endpoint.
   - **Create-monitor aggregate/review payload** (screens 6/14): datasets/files/checks counts, severity
     threshold matrix, dataset preview — confirm a single review/summary endpoint exists or is assembled.
   - **File dataset discovery/listing** for S3 (screen 4): list candidate datasets with schema-verified /
     blocked state + bulk selection support.
   - **SQL rule validate/test** (screens 5/13): `validate SQL` + `test query` returning failed_rows —
     confirm endpoint (vs only `:evaluate`/`:record`).
   - **Notification preferences / channels** (screens 5/13): per-pillar + events + Slack/Email channel
     config — check integrations.py.
   - **Incident silence** (screens 3/18): silence scope/window/note/ticket — check incidents service
     (only ack/resolve/reopen/comments seen so far).
   - **Audit log** (screen 15): filterable events + export + rich event detail (before/after, trace ids) —
     check audit.py.
   - **People & access** (screen 16): members list, roles, permission matrix, service identities —
     check members.py.
   - **Org settings / API keys / governance policies** (screen 17) — check settings.py.
3. **Implement missing endpoints + schemas + services**, add Alembic migration(s) if new tables needed
   (e.g. incident silences, service accounts/API keys, loading-behavior detection cache). Follow
   `ApiResponse` pattern; propagate warnings (per global CLAUDE.md rule 3).
4. **Write end-to-end functional tests** (global CLAUDE.md rule 1 — NO MOCKS; real API calls against the
   app). Tests live under `tests/`. Also ensure `frontend` build still passes.
5. Update `ROADMAP.md` status lines.

## Notes / conventions to honor
- Never edit existing Alembic migrations; add new idempotent ones.
- Don't fail silently — surface non-fatal issues via `ApiResponse.with_warnings` / DTO warning fields.
- Multi-tenancy via `ContextDep` (resolve_context). Local mode uses in-memory SQLite + bootstrap tenant.
