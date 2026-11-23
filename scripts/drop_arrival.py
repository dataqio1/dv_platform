#!/usr/bin/env python3
"""Drop a fresh data file into an arrivals prefix — simulates a new load landing.

This is the "schedule that puts files" piece of the file test kit: run it on a cron/timer
(see scripts/file_arrival.crontab) so a file-arrival freshness monitor keeps seeing new data
and stays green; stop it and the monitor goes stale → warning → severe on its configured grace.

Writes ``load_<UTC-timestamp>.csv`` to the destination, which may be a local directory or an
S3 prefix::

    python scripts/drop_arrival.py --dest s3://dataq-testing-data/observability/arrivals/daily/
    python scripts/drop_arrival.py --dest testdata/file_scenarios/arrivals/daily --rows 50

S3 destinations need boto3 + AWS creds from the usual chain.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import random
from datetime import UTC, datetime
from urllib.parse import urlparse

DEFAULT_DEST = "s3://dataq-testing-data/observability/arrivals/daily/"


def _make_csv(rows: int, stamp: str) -> bytes:
    rng = random.Random()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ROW_ID", "LOAD_TS", "VALUE"])
    for i in range(1, rows + 1):
        w.writerow([i, stamp, rng.randint(1, 1000)])
    return buf.getvalue().encode()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dest", default=DEFAULT_DEST, help="local dir or s3://bucket/prefix/")
    ap.add_argument("--rows", type=int, default=20)
    ap.add_argument("--name", default="load", help="filename stem")
    args = ap.parse_args()

    now = datetime.now(UTC)
    stamp = now.strftime("%Y-%m-%dT%H-%M-%SZ")
    fname = f"{args.name}_{stamp}.csv"
    body = _make_csv(args.rows, now.isoformat())

    if args.dest.startswith("s3://"):
        import boto3

        p = urlparse(args.dest)
        key = f"{p.path.lstrip('/').rstrip('/')}/{fname}"
        boto3.client("s3").put_object(
            Bucket=p.netloc, Key=key, Body=body, ContentType="text/csv"
        )
        print(f"dropped s3://{p.netloc}/{key} ({len(body)} bytes, {args.rows} rows)")
    else:
        os.makedirs(args.dest, exist_ok=True)
        full = os.path.join(args.dest, fname)
        with open(full, "wb") as fh:
            fh.write(body)
        print(f"dropped {full} ({len(body)} bytes, {args.rows} rows)")


if __name__ == "__main__":
    main()
