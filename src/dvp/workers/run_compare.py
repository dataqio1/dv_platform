"""Run ONE compare as a standalone OS process.

Invoked by `dvp worker` (or by hand) as:

    python -m dvp.workers.run_compare --job-id <id>

It builds its own Container — crucially its own Spark session pointed at `spark_master`
(local[*] by default, or a standalone/Connect/K8s cluster) — resolves the job's tenant
from the DB row, and runs the existing compare engine via `CompareService.run_safe`
(which records success/failure on the row and never raises). Exit 0 = succeeded.

Because the DB row carries everything (spec, variables, tenant), no state is passed on the
command line and no secrets touch argv — the process reads credentials from the SecretStore
exactly like the API does. Requires a *shared* metadata DB (file SQLite or Postgres), which
is the server/desktop default; the ephemeral in-memory SQLite used by tests is per-process.
"""

from __future__ import annotations

import argparse
import sys

from dvp.abstractions.factory import build_container
from dvp.config import get_settings
from dvp.core.tenancy import Role, TenantContext
from dvp.db.models import CompareJob
from dvp.testing_engine.service import CompareService


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dvp.workers.run_compare")
    ap.add_argument("--job-id", required=True)
    args = ap.parse_args(argv)

    settings = get_settings()
    container = build_container(settings)
    if settings.is_local:
        container.metadata.create_all()  # idempotent; shared file DB already has the schema
    # This process gets its own Spark session at the configured target.
    container.spark.master = settings.spark_master
    # Incidents opened during the compare should still alert the tenant's channels.
    from dvp.integrations.dispatch import NotificationDispatcher, set_dispatcher

    set_dispatcher(NotificationDispatcher(container))

    with container.metadata.session() as s:  # system session — resolve tenant from the row
        job = s.get(CompareJob, args.job_id)
        if job is None:
            print(f"compare job {args.job_id} not found", file=sys.stderr)
            return 2
        org_id, group_id = job.org_id, job.group_id
        user_id = job.created_by or ""
        variables = dict(job.variables or {})

    ctx = TenantContext.build(org_id=org_id, group_id=group_id, user_id=user_id, role=Role.EDITOR)
    svc = CompareService(container.metadata, container.results, container.secrets, container.spark)
    svc.run_safe(ctx, args.job_id, variables)

    snap = CompareService(container.metadata, container.results, container.secrets, container.spark)
    status = snap.get_job(ctx, args.job_id).get("status")
    print(f"job {args.job_id} -> {status}")
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
