"""LIVE canonicalization tests — real Snowflake + real Oracle, no mocks.

These are the over-the-wire proof for the compare-engine centralization (sql_canon) and the
session-timezone pinning. test_sql_canon.py proves the SQL *text* on sqlite; this file executes the
exact same canonical expressions on the REAL warehouses behind the desktop store's connections and
asserts the two headline claims the audit could not verify in CI:

  1. Each dialect's canonical SQL is VALID and runs on real types (the FF6 timestamp format, the
     boolean CASE, the NUMBER(38,10) numeric form) — a syntax/semantics regression fails here.
  2. For an equal logical value, Snowflake and Oracle render a BYTE-IDENTICAL canonical string — the
     whole point of the cross-engine fingerprint. If they drift, equal rows hash differently.

Plus: the session really is pinned to UTC on both engines (not just that we asked for it).

Skips cleanly (never fails) when the desktop store, a connection, its driver, or the network is
absent — so `pytest` stays green in CI and runs for real wherever the warehouses are reachable.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from dvp.abstractions.secret_store import LocalFileSecretStore
from dvp.connectors.registry import build_connector
from dvp.core.tenancy import Role, TenantContext
from dvp.testing_engine.sql_canon import (
    boolean_text,
    numeric_text,
    string_text,
    temporal_text,
)

REPO = Path(__file__).resolve().parents[2]  # tests/live/ → repo root
DESKTOP_DB = REPO / ".dvp" / "dvp.db"
DESKTOP_SECRETS = REPO / ".dvp" / "secrets"

# A Snowflake TIMESTAMP_NTZ(9) literal and an Oracle TIMESTAMP(6) literal for the SAME instant,
# carrying sub-second precision — the exact case FF6 is meant to preserve (seconds-only truncation
# was the false-match bug). Both render to microseconds → must match byte-for-byte.
_TS_LITERAL = {
    "snowflake": "TO_TIMESTAMP_NTZ('2021-03-04 05:06:07.123456')",
    "oracle": "TIMESTAMP '2021-03-04 05:06:07.123456'",
}
_DATE_LITERAL = {
    "snowflake": "TO_DATE('2021-03-04')",
    "oracle": "DATE '2021-03-04'",
}


# ----------------------------- gating / live connectors -----------------------------
def _store_or_skip():
    if not DESKTOP_DB.exists() or not DESKTOP_SECRETS.exists():
        pytest.skip("no desktop store (.dvp/dvp.db + secrets) — live warehouses unavailable")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """Live connectors by dialect, built from a THROWAWAY copy of the desktop store (so a test
    never mutates the real store/secrets). Each connector is verified reachable once; dialects
    whose connection/driver/network is absent simply don't appear in the map."""
    _store_or_skip()
    work = tmp_path_factory.mktemp("canon_live")
    db = work / "dvp.db"
    secrets = work / "secrets"
    shutil.copy(DESKTOP_DB, db)
    shutil.copytree(DESKTOP_SECRETS, secrets)

    store = LocalFileSecretStore(str(secrets))
    conn = sqlite3.connect(db)
    # One representative live connection per dialect we canonicalize for.
    wanted = {"snowflake": "snowflake-dwh-test", "oracle": "oracle-dataq"}
    connectors: dict[str, object] = {}
    for dialect, name in wanted.items():
        row = conn.execute(
            "SELECT source_type, coordinates, auth_method, secret_ref, org_id, group_id "
            "FROM connection WHERE name=? LIMIT 1",
            (name,),
        ).fetchone()
        if not row:
            continue
        source_type, coords, auth, secret_ref, org_id, group_id = row
        ctx = TenantContext.build(org_id, group_id, "live-test", Role.GROUP_ADMIN)
        try:
            secret = store.get(ctx, secret_ref) if secret_ref else None
            c = build_connector(source_type, json.loads(coords or "{}"), auth, secret)
            # Reachability probe: a dialect-appropriate 1-row select.
            c.execute("SELECT 1 FROM DUAL" if dialect == "oracle" else "SELECT 1")
        except Exception as exc:  # noqa: BLE001 - absent driver / unreachable → omit, don't fail
            print(f"[canon-live] {dialect} unavailable: {str(exc)[:160]}")
            continue
        connectors[dialect] = c
    conn.close()
    if not connectors:
        pytest.skip("no live warehouse reachable (snowflake-dwh-test / oracle-dataq)")
    return connectors


def _scalar(connector, dialect: str, expr: str):
    """Evaluate a scalar expression on the live engine and return the single value."""
    sql = f"SELECT {expr} FROM DUAL" if dialect == "oracle" else f"SELECT {expr}"
    res = connector.execute(sql)
    return res.rows[0][0]


def _canon_of(connector, dialect: str, canon_sql: str):
    val = _scalar(connector, dialect, canon_sql)
    return None if val is None else str(val)


# ----------------------------- session timezone (per engine) -----------------------------
def test_snowflake_session_timezone_is_utc(live):
    sf = live.get("snowflake") or pytest.skip("snowflake not reachable")
    # Snowflake has no CURRENT_TIMEZONE() scalar — read the session parameter directly. SHOW
    # PARAMETERS returns columns key,value,default,...; the effective value must be the UTC we pin.
    res = sf.execute("SHOW PARAMETERS LIKE 'TIMEZONE' IN SESSION")
    idx = res.columns.index("value") if "value" in res.columns else 1
    assert res.rows[0][idx] == "UTC"


def test_oracle_session_timezone_is_utc(live):
    ora = live.get("oracle") or pytest.skip("oracle not reachable")
    # ALTER SESSION SET TIME_ZONE='UTC' → SESSIONTIMEZONE reads back 'UTC' (or '+00:00').
    assert _scalar(ora, "oracle", "SESSIONTIMEZONE") in ("UTC", "+00:00")


# ----------------------------- canonical SQL executes on real types -----------------------------
@pytest.mark.parametrize("dialect", ["snowflake", "oracle"])
def test_numeric_canonical_runs_live(dialect, live):
    c = live.get(dialect) or pytest.skip(f"{dialect} not reachable")
    assert _canon_of(c, dialect, numeric_text(dialect, "1.50")) == "1.5"
    assert _canon_of(c, dialect, numeric_text(dialect, "100.00")) == "100"
    # 18-digit id must survive with full precision (no DOUBLE collapse).
    assert _canon_of(c, dialect, numeric_text(dialect, "123456789012345678")) == "123456789012345678"


@pytest.mark.parametrize("dialect", ["snowflake", "oracle"])
def test_temporal_canonical_runs_live(dialect, live):
    c = live.get(dialect) or pytest.skip(f"{dialect} not reachable")
    ts = _canon_of(c, dialect, temporal_text(dialect, _TS_LITERAL[dialect], is_date=False))
    dt = _canon_of(c, dialect, temporal_text(dialect, _DATE_LITERAL[dialect], is_date=True))
    assert ts == "2021-03-04 05:06:07.123456"   # FF6 preserved the sub-second component
    assert dt == "2021-03-04"                    # DATE stays calendar-only


@pytest.mark.parametrize("dialect", ["snowflake", "oracle"])
def test_boolean_canonical_runs_live(dialect, live):
    c = live.get(dialect) or pytest.skip(f"{dialect} not reachable")
    # NUMBER(1)=1 / 'Y' / 'true' → 'true'; 0 → 'false' on both engines.
    assert _canon_of(c, dialect, boolean_text(dialect, "1")) == "true"
    assert _canon_of(c, dialect, boolean_text(dialect, "0")) == "false"
    assert _canon_of(c, dialect, boolean_text(dialect, "'Y'")) == "true"


@pytest.mark.parametrize("dialect", ["snowflake", "oracle"])
def test_string_canonical_runs_live(dialect, live):
    c = live.get(dialect) or pytest.skip(f"{dialect} not reachable")
    assert _canon_of(c, dialect, string_text(dialect, "'  hi  '")) == "hi"   # trimmed
    assert _canon_of(c, dialect, string_text(dialect, "''")) is None          # '' → NULL


# ----------------------------- THE headline: cross-engine byte-identical -----------------------------
def test_cross_engine_canonical_is_byte_identical(live):
    """For each logical value, Snowflake and Oracle must produce the SAME canonical string — else the
    cross-engine fingerprint prunes/over-drills wrongly. This is the claim CI alone cannot check."""
    sf, ora = live.get("snowflake"), live.get("oracle")
    if not sf or not ora:
        pytest.skip("need BOTH snowflake-dwh-test and oracle-dataq reachable")

    def both(canon_sf: str, canon_ora: str):
        return (_canon_of(sf, "snowflake", canon_sf), _canon_of(ora, "oracle", canon_ora))

    # numeric: trailing-zero-normalized, full precision
    for v in ("1.50", "100.00", "0", "123456789012345678", "-42.4200"):
        s, o = both(numeric_text("snowflake", v), numeric_text("oracle", v))
        assert s == o, f"numeric {v!r}: snowflake={s!r} oracle={o!r}"

    # timestamp to the microsecond (the sub-second case)
    s, o = both(
        temporal_text("snowflake", _TS_LITERAL["snowflake"], is_date=False),
        temporal_text("oracle", _TS_LITERAL["oracle"], is_date=False),
    )
    assert s == o == "2021-03-04 05:06:07.123456"

    # boolean
    for v in ("1", "0", "'Y'"):
        s, o = both(boolean_text("snowflake", v), boolean_text("oracle", v))
        assert s == o, f"boolean {v!r}: snowflake={s!r} oracle={o!r}"
