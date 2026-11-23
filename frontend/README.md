# DV Platform — Web UI

React + Vite + TypeScript + Tailwind SPA for the DV Platform Data Observability
features. It talks to the FastAPI backend at `/api/v1` (proxied in dev).

## Run

Start the backend first (from the repo root):

```bash
PYTHONPATH="$(pwd)/src" .venv/bin/dvp run     # http://127.0.0.1:8000
```

Then the UI:

```bash
cd frontend
npm install
npm run dev                                    # http://127.0.0.1:5173
```

The dev server proxies `/api` → `http://127.0.0.1:8000` (override with
`DVP_API_URL`). Local mode needs no auth — identity is auto-provisioned.

## What's here

| Page | Route | Mirrors |
|------|-------|---------|
| Data Observability (monitors + table health) | `/observability` | — |
| Create Monitor (watermark auto-detect + validate) | `/observability/new` | `create_water_mark.png` |
| Table observability (4 pillars + history + column drill-down) | `/observability/:monitorId/tables/:tableRef` | `each_table_observability_metric.png` |
| Connections | `/connections` | — |

### Quick demo
1. **Connections** → *Add sample inline source* (RAW_SALES with two tables — no warehouse needed).
2. **Data Observability** → *New monitor* → pick the connection → *Auto-suggest from schema* (detects `INGESTED_AT`), watch the per-table validation → *Create & profile*.
3. Land on the table page: four pillar cards, click a pillar for history, pick a column for its drill-down, hit *Profile now* to refresh.

## Build

```bash
npm run build      # tsc typecheck + vite production build → dist/
```
