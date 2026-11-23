# File-source test kit for data observability

A complete, reproducible set of **file** datasets (local + S3) that exercises every format
and layout the platform's `FileConnector` supports, plus the monitors, alert policies, and a
file-arrival **schedule** that drive freshness/volume/schema/quality alerting for files.

Everything here is generated from one source of truth — `scripts/gen_file_scenarios.py` — so
the local fixtures, the S3 objects, and the pytest suite all see identical data.

## Scenario matrix

| Path | Format | Layout | Exercises |
|------|--------|--------|-----------|
| `single/orders.csv` | CSV | single file | typed cols, nulls → quality, `ORDER_TS` |
| `single/customers.tsv` | TSV | single file | tab delimiter |
| `single/events.ndjson` | NDJSON | single file | newline-delimited JSON |
| `single/products.json` | JSON array | single file | array form, **BOOLEAN** inference |
| `single/inventory.json` | JSON `{"records":[…]}` | single file | wrapped form |
| `lake/regions.csv` | CSV | scan → table | a file one level below a scanned prefix |
| `lake/shipments/part-*.csv` + `_SUCCESS` | CSV | scan → folder union | part-file union; Spark marker ignored |
| `lake/payments/part-*.ndjson` + `.hidden.csv` | NDJSON | scan → folder union | union; hidden file ignored |
| `typed/types_sample.csv` | CSV | single file | NUMBER/DATE/TIMESTAMP/VARCHAR inference |
| `typed/with_nulls.csv` | CSV | single file | ~50% nulls → quality score 75 |
| `parquet/sales.parquet` | Parquet | single file | Parquet read, real BOOLEAN/TIMESTAMP |
| `arrivals/daily/`, `arrivals/hourly/` | CSV | scan → folder union | **freshness** driven by the schedule |

Covered behaviors: CSV/TSV/NDJSON/JSON(array+records)/Parquet; single file, folder-of-parts
union, prefix scan (files **and** sub-folders become tables); `_SUCCESS`/`.crc`/hidden files
skipped; type inference across all canonical types; null-rate → quality; and file-arrival
freshness (newest file's modified time / S3 `LastModified`) with per-monitor warn/severe grace.

## S3 location

Everything is mirrored under **`s3://dataq-testing-data/observability/`** (us-east-1):

```
observability/
  single/        orders.csv customers.tsv events.ndjson products.json inventory.json
  lake/          regions.csv  shipments/part-*.csv (+_SUCCESS)  payments/part-*.ndjson (+.hidden)
  typed/         types_sample.csv  with_nulls.csv
  parquet/       sales.parquet
  arrivals/      daily/  hourly/     (grow over time via the schedule)
```

## How to use

```bash
# 0) regenerate the local tree (deterministic; parquet needs pyarrow)
python scripts/gen_file_scenarios.py --clean

# 1) (re)seed S3 — idempotent; --dry-run to preview
python scripts/seed_s3_file_scenarios.py

# 2) create monitors over the S3 scenarios (API must be running; server needs boto3 + AWS creds)
PYTHONPATH=src .venv/bin/dvp run          # in another shell
python scripts/seed_file_monitors.py       # 6 monitors, each profiled once

# 3) schedule file arrivals so the "Scheduled arrivals" monitor stays fresh
python scripts/drop_arrival.py --dest s3://dataq-testing-data/observability/arrivals/daily/
#   install the recurring schedule (edit the repo path inside the file first):
( crontab -l 2>/dev/null; sed 's#/REPO#'"$PWD"'#' scripts/file_arrival.crontab ) | crontab -
```

**Freshness demo:** the *Scheduled arrivals* monitor uses a tight grace (warn 2h / severe 6h).
Keep the cron running and it stays green; stop it and freshness goes **warning** then **severe**
— the same incident pipeline (dedup, auto-resolve, severity routing) used for warehouse tables.

## Monitors created by `seed_file_monitors.py`

| Monitor | Source | Freshness policy (file arrival) |
|---------|--------|---------------------------------|
| Orders file (CSV) | `single/orders.csv` | cadence 24h, warn 26h, severe 48h |
| Customers file (TSV) | `single/customers.tsv` | 24 / 26 / 48 |
| Events file (NDJSON) | `single/events.ndjson` | 1 / 2 / 6 |
| Sales file (Parquet) | `parquet/sales.parquet` | 24 / 26 / 48 |
| Data lake (scan prefix) | `lake/` → REGIONS, SHIPMENTS, PAYMENTS | 24 / 26 / 48 |
| Scheduled arrivals | `arrivals/` → DAILY, HOURLY | 1 / 2 / 6 (tied to the cron) |

## Tests

`tests/test_file_scenarios.py` validates every scenario end-to-end (no mocks) against the real
`FileConnector` and the observability API using locally-generated copies of these exact files;
`tests/test_file_observability.py` covers file-arrival freshness severity. Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_file_scenarios.py tests/test_file_observability.py -q
```

> Note: the local dev DB (`.dvp/dvp.db`) self-heals new columns on boot (`create_all`), so after
> pulling this feature just restart `dvp run` — no manual migration needed locally. Server/prod
> applies `migrations/versions/0019_monitor_alert_policy.py` via Alembic.
