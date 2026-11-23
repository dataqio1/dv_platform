"""Empirical probe: does Oracle STANDARD_HASH('x','SHA256') == Snowflake SHA2('x',256)?

Both are SHA-256, so equal INPUT BYTES must yield equal digests. The only risks are
(a) character-set encoding of the input and (b) how each engine renders the digest
(Oracle returns RAW -> uppercase hex; Snowflake returns lowercase hex). We LOWER() Oracle
and compare against a Python hashlib.sha256 reference computed here.

Run:  PYTHONPATH=src DVP_PROFILE=local .venv/bin/python scripts/hash_probe.py
"""

from __future__ import annotations

import hashlib

from dvp.abstractions.factory import build_container
from dvp.config import get_settings
from dvp.connectors.registry import build_connector
from dvp.db.models import Connection
from dvp.services.tenancy import bootstrap_local_tenant, resolve_context

ORACLE_CONN = "16a1a10c-a53f-4937-b73e-8b010330488a"  # oracle-dataq
SNOWFLAKE_CONN = "1fea1879-f30c-47a7-80b8-c1ffa37a7d22"  # snowflake-dhiraj

# Tricky inputs to stress encoding + special chars (keep ASCII-only first, then unicode).
PROBES = [
    "a|b|1",
    "",
    "hello world",
    "1.5|2024-01-01|NULLish",
    "pipe|inside|value",
    "café|münchen|naïve",  # non-ASCII -> tests DB charset (AL32UTF8 vs 8-bit)
    "x" * 1000,  # long but under 4000-byte STANDARD_HASH limit
]


def get_connector(container, ctx, conn_id):
    with container.metadata.session(ctx) as s:
        c = s.get(Connection, conn_id)
        source_type, coords = c.source_type, dict(c.coordinates)
        auth, secret_ref = c.auth_method, c.secret_ref
    secret = container.secrets.get(ctx, secret_ref) if secret_ref and container.secrets else None
    return build_connector(source_type, coords, auth, secret)


def sql_lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def main() -> None:
    settings = get_settings()
    container = build_container(settings)
    container.metadata.create_all()
    t = bootstrap_local_tenant(container.metadata, settings)
    ctx = resolve_context(
        container.metadata, user_id=t.user_id, org_id=t.org_id, group_id=t.group_id
    )

    ora = get_connector(container, ctx, ORACLE_CONN)
    sf = get_connector(container, ctx, SNOWFLAKE_CONN)

    print(f"{'input':<22} {'py==ora':>8} {'py==sf':>7} {'ora==sf':>8}")
    print("-" * 52)
    all_ok = True
    for p in PROBES:
        py = hashlib.sha256(p.encode("utf-8")).hexdigest()
        ora_sql = f"SELECT LOWER(STANDARD_HASH({sql_lit(p)}, 'SHA256')) FROM dual"
        sf_sql = f"SELECT SHA2({sql_lit(p)}, 256)"
        ora_h = str(ora.execute(ora_sql).rows[0][0]).lower()
        sf_h = str(sf.execute(sf_sql).rows[0][0]).lower()
        a, b, c = py == ora_h, py == sf_h, ora_h == sf_h
        all_ok = all_ok and a and b and c
        label = (p[:19] + "...") if len(p) > 22 else p
        print(f"{label:<22} {str(a):>8} {str(b):>7} {str(c):>8}")
        if not (a and b and c):
            print(f"    py :  {py}")
            print(f"    ora:  {ora_h}")
            print(f"    sf :  {sf_h}")

    print("-" * 52)
    print("RESULT:", "ALL MATCH ✅" if all_ok else "MISMATCH ❌ (see rows above)")


if __name__ == "__main__":
    main()
