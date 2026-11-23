#!/usr/bin/env python3
"""Upload the generated file-scenario tree to S3 for data-observability testing.

Generates the tree with gen_file_scenarios.py (into a temp dir) and mirrors it to
``s3://<bucket>/<prefix>/`` so the monitors created by seed_file_monitors.py have real
files to profile. Idempotent — object keys are stable, so re-running overwrites in place.

Usage::

    # dry run — show what would upload, touch nothing
    python scripts/seed_s3_file_scenarios.py --dry-run

    # seed the default bucket/prefix (s3://dataq-testing-data/observability/)
    python scripts/seed_s3_file_scenarios.py

    python scripts/seed_s3_file_scenarios.py --bucket my-bucket --prefix my/prefix

AWS creds come from the usual chain (env / shared config / instance role). Needs boto3.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import tempfile

DEFAULT_BUCKET = "dataq-testing-data"
DEFAULT_PREFIX = "observability"


def _gen(root: str) -> None:
    import importlib.util

    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "gen_file_scenarios", os.path.join(here, "gen_file_scenarios.py")
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    mod.generate(root, clean=True)


def _iter_files(root: str):
    for dirpath, _dirs, files in os.walk(root):
        for f in sorted(files):
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            yield full, rel


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bucket", default=DEFAULT_BUCKET)
    ap.add_argument("--prefix", default=DEFAULT_PREFIX, help="key prefix (no leading/trailing /)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    prefix = args.prefix.strip("/")

    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "file_scenarios")
        _gen(root)
        files = list(_iter_files(root))
        print(f"\n{'DRY RUN — ' if args.dry_run else ''}uploading {len(files)} files "
              f"→ s3://{args.bucket}/{prefix}/")

        client = None
        if not args.dry_run:
            import boto3

            client = boto3.client("s3")

        for full, rel in files:
            key = f"{prefix}/{rel}"
            size = os.path.getsize(full)
            if args.dry_run:
                print(f"  would put s3://{args.bucket}/{key} ({size} bytes)")
                continue
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as fh:
                client.put_object(Bucket=args.bucket, Key=key, Body=fh.read(), ContentType=ctype)
            print(f"  put s3://{args.bucket}/{key} ({size} bytes)")

    print("\nDone." if not args.dry_run else "\nDry run complete.")
    print("Prefixes now available for monitors (scan one level below):")
    print(f"  s3://{args.bucket}/{prefix}/single/     single-file datasets")
    print(f"  s3://{args.bucket}/{prefix}/lake/       scan-prefix: REGIONS, SHIPMENTS, PAYMENTS")
    print(f"  s3://{args.bucket}/{prefix}/parquet/    parquet dataset")
    print(f"  s3://{args.bucket}/{prefix}/arrivals/   freshness arrivals (see drop_arrival.py)")


if __name__ == "__main__":
    main()
