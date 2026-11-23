# Live (real-data) tests

Tests in this folder run against **real external systems** — live Snowflake, live Oracle, and real
S3 — using the established connections in the desktop store (`.dvp/dvp.db` + `.dvp/secrets`). No
mocks, no fakes.

Every test here **skips cleanly** (never fails) when the store, a connection, its driver, or the
network is unavailable — so they stay green in CI and only run for real where the warehouses are
reachable. That's why they live in a separate folder: they're data-dependent and hit the network,
unlike the deterministic unit tests under `tests/`.

## Files
- `test_canon_live.py` — executes the shared compare canonicalization (`sql_canon`) on real
  Snowflake + Oracle and asserts the two claims CI alone can't: the canonical SQL is valid on real
  types (FF6 timestamps, boolean CASE, NUMBER(38,10)), and equal values render **byte-identical**
  across engines. Also verifies the session time zone is really pinned to UTC.
- `test_live_warehouse_s3.py` — the data-observability backend contract end-to-end over real
  Snowflake + S3.

## Running
```bash
# Just the live suite
pytest tests/live -v

# One file
pytest tests/live/test_canon_live.py -v -rs   # -rs shows skip reasons

# The default `pytest` run still discovers these (they skip without a store).
```

## Prerequisites for a real run
- Desktop store present: `.dvp/dvp.db` + `.dvp/secrets/`
- Connections `snowflake-dwh-test` (and `oracle-dataq` for the cross-engine checks) reachable
- Drivers installed: `snowflake-connector-python`, `oracledb`
- The Oracle account must be unlocked (`ORA-28000: account is locked` → a DBA must
  `ALTER USER <user> ACCOUNT UNLOCK`); otherwise the Oracle + cross-engine tests skip.
