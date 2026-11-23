"""CompareService — persists compare jobs and serves results.

Job *execution* against real Snowflake/Oracle is delegated to the connectors +
Spark path (not implemented in the scaffold); the service layer, job lifecycle,
and result persistence/drill-down are fully wired. The pure `compare_rows` engine
is used directly for in-memory datasets and tests.
"""

from __future__ import annotations

import hashlib
import json
import time

from sqlalchemy import select

from dvp.abstractions.result_store import ResultStore, parse_warehouse_ref
from dvp.connectors.base import TableRef
from dvp.core.cron import is_valid as cron_is_valid
from dvp.core.cron import next_due
from dvp.core.errors import NotFoundError, ValidationError
from dvp.core.logging import get_logger
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.base import new_id, utcnow
from dvp.db.models import CompareJob, CompareResult, CompareRun, Connection, Project, User
from dvp.db.session import MetadataStore
from dvp.services import audit
from dvp.testing_engine.cancel import CompareCancelled
from dvp.testing_engine.keydetect import ColumnMeta, detect_key
from dvp.testing_engine.rowcompare import CompareOptions, compare_rows
from dvp.testing_engine.sql_guard import validate_compare_side
from dvp.testing_engine.typemap import canonicalize
from dvp.testing_engine.variables import extract_variables, missing_variables, substitute

log = get_logger("dvp.compare")

# Budget guards so a compare can never run unbounded on huge warehouse tables.
DEFAULT_COMPARE_TIMEOUT_S = 1800  # 30 min wall-clock ceiling per compare (0 in options = off)
DEFAULT_MAX_QUERIES = 5000        # hard cap on pushdown queries per compare

# Rough per-cell cost of holding a pulled row in Python: the value object + its dict slot, plus
# the intermediate keyed structures the compare builds for BOTH sides at once (≈1.6×).
_EST_CELL_BYTES = 90
_EST_OVERHEAD = 1.6
# A pushdown run (Snowflake hash / in-database recursive) keeps only a small sampled/aggregated
# footprint in local RAM, so it reserves this little from the pool instead of a full in-memory
# budget — letting many pushdown compares run concurrently.
_PUSHDOWN_MEMORY_GB = 0.25

# In auto mode, a cross-database compare switches from "load both sides into memory" to the
# streaming key-range engine once the two tables together exceed this many rows.
_STREAM_ROW_THRESHOLD = 250_000


def _stream_window_rows(memory_gb: float | None, ncols: int) -> int:
    """Rows per streaming window. Sized so a window from BOTH sides fits comfortably in the memory
    budget (half of it, for headroom); defaults to 50k rows when no budget is set. Clamped so a
    window is always a sane batch regardless of column count."""
    if memory_gb and memory_gb > 0:
        rows_both = (0.5 * memory_gb * (1024 ** 3)) / (max(1, ncols) * _EST_CELL_BYTES * _EST_OVERHEAD)
        return int(max(10_000, min(500_000, rows_both / 2)))
    return 50_000


class _MemoryBudgetExceeded(Exception):
    """The in-memory compare would need more RAM than the test case's memory budget. Raised so the
    run is recorded as FAILED with a clear, actionable message (not re-raised like a 4xx)."""


def _estimate_inmemory_gb(total_rows: int, ncols: int) -> float:
    return total_rows * max(1, ncols) * _EST_CELL_BYTES * _EST_OVERHEAD / (1024 ** 3)


def _truthy(v: object) -> bool:
    return str(v).strip().lower() not in ("false", "0", "no", "off", "none", "")


class RunLog:
    """Timestamped OPERATIONAL trace of one compare run — phases, engine choice, timing and
    warnings. Deliberately does NOT restate the run's result (match verdict, row counts, diffs):
    that lives in the result and is shown in its own panel. Stored on the job (succeeded OR
    failed), shown and downloadable in the UI, and deleted with the job."""

    def __init__(self) -> None:
        self.lines: list[dict] = []
        self._t0 = time.monotonic()

    def log(self, msg: str, level: str = "info") -> None:
        self.lines.append(
            {
                "ts": utcnow().isoformat(),
                "elapsed_ms": int((time.monotonic() - self._t0) * 1000),
                "level": level,
                "msg": str(msg),
            }
        )

    def notes(self, notes, level: str = "info") -> None:  # noqa: ANN001
        for n in notes or []:
            self.log(n, level)


def _close_connectors(*conns) -> None:  # noqa: ANN002
    """Release reused warehouse sessions after a compare (dedup — the recursive engine uses
    one connector for both sides). Best-effort; a close failure never fails the compare."""
    for c in {id(c): c for c in conns if c is not None}.values():
        if hasattr(c, "close"):
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


class CompareService:
    def __init__(
        self, store: MetadataStore, results: ResultStore, secrets=None, spark=None, cancel_registry=None
    ) -> None:  # noqa: ANN001
        self.store = store
        self.results = results
        self.secrets = secrets
        self.spark = spark  # SparkSessionProvider for scale-out compares (optional)
        self.cancel_registry = cancel_registry  # shared across API/worker threads (optional)

    def create_job(
        self,
        ctx: TenantContext,
        *,
        kind: str,
        source: dict,
        target: dict,
        options: dict | None = None,
        name: str | None = None,
        batch_id: str | None = None,
        batch_name: str | None = None,
        project_id: str | None = None,
        source_system: str | None = None,
        layer: str | None = None,
        tags: list | None = None,
    ) -> dict:
        ctx.require(Permission.WRITE)
        # Reject unsafe SQL fragments at authoring time (fast feedback); run() re-checks after
        # variable substitution, which is the real security boundary.
        validate_compare_side(source, side="source")
        validate_compare_side(target, side="target")
        with self.store.session(ctx) as s:
            # A test case always lives in a project: validate the one given, else fall back
            # to the group's default ("General") project so a test case is never orphaned.
            if project_id:
                proj = s.get(Project, project_id)
                if proj is None or proj.group_id != ctx.group_id or proj.archived_at is not None:
                    raise ValidationError(
                        "project not found or archived", details={"project_id": project_id}
                    )
            else:
                project_id = _ensure_default_project(s, ctx)
            job = CompareJob(
                org_id=ctx.org_id,
                group_id=ctx.group_id,
                name=name,
                batch_id=batch_id,
                batch_name=batch_name,
                project_id=project_id,
                source_system=source_system,
                layer=layer,
                tags=tags or [],
                kind=kind,
                source=source,
                target=target,
                options=options or {},
                status="queued",
                created_by=ctx.user_id,
            )
            s.add(job)
            s.flush()
            audit.record(s, ctx, "compare.create", entity_type="compare_job", entity_id=job.id)
            return _job_dict(job)

    def assign(
        self,
        ctx: TenantContext,
        job_id: str,
        *,
        project_id: str | None = None,
        source_system: str | None = None,
        layer: str | None = None,
        tags: list | None = None,
    ) -> dict:
        """Set this test case's project / grouping taxonomy / tags."""
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            job = self._load(s, ctx, job_id)
            if project_id is not None:
                job.project_id = project_id or None
            if source_system is not None:
                job.source_system = source_system or None
            if layer is not None:
                job.layer = layer or None
            if tags is not None:
                job.tags = tags
            audit.record(s, ctx, "compare.assign", entity_type="compare_job", entity_id=job.id)
            return _job_dict(job)

    # The parts of a test case's definition that an edit may change. Taxonomy (project/system/
    # layer/tags) and schedule have their own endpoints and are intentionally not listed here.
    _EDITABLE_FIELDS = ("name", "batch_name", "kind", "source", "target", "options")

    def update_job(self, ctx: TenantContext, job_id: str, fields: dict) -> dict:
        """Edit a test case's definition in place. Only keys present in ``fields`` are applied
        (partial update); the stored run history and schedule are left untouched."""
        ctx.require(Permission.WRITE)
        if "source" in fields:
            validate_compare_side(fields["source"], side="source")
        if "target" in fields:
            validate_compare_side(fields["target"], side="target")
        with self.store.session(ctx) as s:
            job = self._load(s, ctx, job_id)
            for key in self._EDITABLE_FIELDS:
                if key in fields:
                    setattr(job, key, fields[key])
            audit.record(s, ctx, "compare.update", entity_type="compare_job", entity_id=job.id)
            return _job_dict(job)

    def get_job(self, ctx: TenantContext, job_id: str) -> dict:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            job = self._load(s, ctx, job_id)
            result = s.scalar(select(CompareResult).where(CompareResult.compare_job_id == job_id))
            d = _job_dict(job)
            d["result"] = _result_dict(result) if result else None
            self._attach_actor_names(s, [d])
            return d

    def sample_by_key(
        self,
        ctx: TenantContext,
        source_spec: dict,
        target_spec: dict,
        *,
        key: list[str],
        key_values: list[list] | None = None,
        columns: list[str] | None = None,
        column_mappings: dict[str, str] | None = None,
        limit: int = 20,
    ) -> dict:
        """Fetch rows for a set of keys from BOTH source and target, aligned by key, so the UI can
        eyeball sample data side by side.

        The keys are either sampled from the source (``key_values`` omitted — pick ~``limit`` keys)
        or supplied explicitly by the user (``key_values`` — look up exactly those). Works for any
        side (table / SQL / file-on-S3) because it goes through the same connector reads the compare
        engine uses. Key matching is type-tolerant (a key read as ``123`` on one side lines up with
        ``"123"`` on the other) so heterogeneous sources still align."""
        ctx.require(Permission.READ)
        limit = max(1, min(int(limit), 100))
        mappings = column_mappings or {}

        src_conn, src_from, src_tref = self._side(ctx, source_spec)
        dst_conn, dst_from, dst_tref = self._side(ctx, target_spec)
        src_pred = source_spec.get("filter")
        dst_pred = target_spec.get("filter")

        # No key yet → rows can't be lined up, so return an independent sample from EACH side.
        if not key:
            def _side_sample(conn, from_sql, tref, pred) -> dict:  # noqa: ANN001
                cols = [c.name for c in self._columns(conn, from_sql, tref, pred)]
                rows = self._pull(conn, from_sql, cols, pred, limit=limit)
                return {"columns": cols, "rows": [[r.get(c) for c in cols] for r in rows]}

            warnings: list[str] = []
            source = _side_sample(src_conn, src_from, src_tref, src_pred)
            try:
                target = _side_sample(dst_conn, dst_from, dst_tref, dst_pred)
            except Exception as exc:  # noqa: BLE001 — surface, don't crash the preview
                target = {"columns": [], "rows": []}
                warnings.append(f"Could not read a sample from the target: {exc}")
            return {"aligned": False, "source": source, "target": target, "warnings": warnings}

        # Columns to display (default: all source columns), key columns always included.
        src_schema = self._columns(src_conn, src_from, src_tref, src_pred)
        show_src = columns or [c.name for c in src_schema]
        tgt_key = [mappings.get(k, k) for k in key]

        def q(c: str) -> str:
            return '"' + c.replace('"', '""') + '"'

        def lit(v: object) -> str:
            if v is None:
                return "NULL"
            if isinstance(v, bool):
                return "1" if v else "0"
            if isinstance(v, (int, float)):
                return repr(v)
            return "'" + str(v).replace("'", "''") + "'"

        def key_predicate(cols: list[str], tuples: list[tuple]) -> str:
            return " OR ".join(
                "(" + " AND ".join(f"{q(c)} = {lit(v)}" for c, v in zip(cols, t, strict=False)) + ")"
                for t in tuples
            )

        def both(a: str | None, b: str) -> str:
            return f"({a}) AND ({b})" if a else b

        def match_key(vals: tuple) -> tuple:
            # Normalise so a numeric key from one side lines up with a string key from the other.
            out = []
            for v in vals:
                if v is None:
                    out.append("\x00")
                elif isinstance(v, float) and v.is_integer():
                    out.append(str(int(v)))
                else:
                    out.append(str(v))
            return tuple(out)

        col_pairs = [{"src": c, "dst": mappings.get(c, c)} for c in show_src]

        # 1) Decide which keys to look up: the user's explicit values, or a cheap sample of the source.
        provided = bool(key_values)
        if provided:
            # Coerce the typed-in values to the source key column types so a "1001" typed by the
            # user lines up with a numeric key column (and the SQL literal is well-formed).
            key_types = {c.name: canonicalize(c.data_type).value for c in src_schema}
            key_tuples = _coerce_key_values(key, key_values, key_types)[:limit]
            if not key_tuples:
                return {"aligned": True, "provided": True, "key": key, "columns": col_pairs,
                        "sampled": 0, "rows": [],
                        "warnings": ["No usable keys provided — every key column needs a value."]}
        else:
            # Sample key values from the source (LIMIT-based — cheap and works for every source type).
            sampled = self._pull(src_conn, src_from, key, src_pred, limit=limit)
            key_tuples = [
                t for t in (tuple(r.get(k) for k in key) for r in sampled)
                if all(v is not None for v in t)
            ]
            if not key_tuples:
                return {"aligned": True, "provided": False, "key": key, "columns": col_pairs,
                        "sampled": 0, "rows": [], "warnings": ["No sample keys found in the source."]}

        # 2) Fetch the rows for those keys from both sides (same key VALUES, per-side key NAMES).
        src_cols = list(dict.fromkeys([*key, *show_src]))
        tgt_cols = list(dict.fromkeys([*tgt_key, *[mappings.get(c, c) for c in show_src]]))
        src_rows = self._pull(src_conn, src_from, src_cols, both(src_pred, key_predicate(key, key_tuples)))
        warnings: list[str] = []
        try:
            dst_rows = self._pull(dst_conn, dst_from, tgt_cols, both(dst_pred, key_predicate(tgt_key, key_tuples)))
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the preview
            dst_rows = []
            warnings.append(f"Could not read the target for these keys: {exc}")

        src_by = {match_key(tuple(r.get(k) for k in key)): r for r in src_rows}
        dst_by = {match_key(tuple(r.get(tk) for tk in tgt_key)): r for r in dst_rows}

        rows = []
        missing = 0
        for t in key_tuples:
            mk = match_key(t)
            s_row = src_by.get(mk)
            d_row = dst_by.get(mk)
            if s_row is None and d_row is None:
                missing += 1
            rows.append({
                "key": dict(zip(key, t, strict=False)),
                "source": {c: (s_row or {}).get(c) for c in show_src} if s_row else None,
                "target": {mappings.get(c, c): (d_row or {}).get(mappings.get(c, c)) for c in show_src} if d_row else None,
            })
        # When the user asked for specific keys, tell them plainly if some matched nothing on either
        # side (a typo or a key that simply doesn't exist) rather than leaving a silent blank row.
        if provided and missing:
            warnings.append(f"{missing} of {len(key_tuples)} key(s) were not found in source or target.")
        return {"aligned": True, "provided": provided, "key": key, "columns": col_pairs,
                "sampled": len(key_tuples), "rows": rows, "warnings": warnings}

    def dry_run_compare(
        self,
        ctx: TenantContext,
        source_spec: dict,
        target_spec: dict,
        *,
        key: list[str],
        columns: list[str] | None = None,
        column_mappings: dict[str, str] | None = None,
        limit: int = 1000,
    ) -> dict:
        """Run the REAL data compare on a small sample of ~``limit`` keys (sampled from source,
        fetched from both sides, aligned by key) WITHOUT persisting anything — so the user can
        catch a wrong key / bad mapping / everything-mismatches before a full, expensive run.
        Works for table / SQL / file sides (same connector path)."""
        ctx.require(Permission.READ)
        if not key:
            raise ValidationError("a dry run needs the key column(s)")
        limit = max(1, min(int(limit), 10000))
        mappings = column_mappings or {}
        src_conn, src_from, src_tref = self._side(ctx, source_spec)
        dst_conn, dst_from, dst_tref = self._side(ctx, target_spec)
        src_pred = source_spec.get("filter")
        dst_pred = target_spec.get("filter")

        src_schema = self._columns(src_conn, src_from, src_tref, src_pred)
        src_types = {c.name: c.data_type for c in src_schema}
        show_src = columns or [c.name for c in src_schema]
        compare_cols = [c for c in show_src if c not in set(key)]

        def q(c: str) -> str:
            return '"' + c.replace('"', '""') + '"'

        def lit(v: object) -> str:
            if v is None:
                return "NULL"
            if isinstance(v, bool):
                return "1" if v else "0"
            if isinstance(v, (int, float)):
                return repr(v)
            return "'" + str(v).replace("'", "''") + "'"

        def key_pred(cols: list[str], tuples: list[tuple]) -> str:
            return " OR ".join(
                "(" + " AND ".join(f"{q(c)} = {lit(v)}" for c, v in zip(cols, t, strict=False)) + ")"
                for t in tuples
            )

        def both(a: str | None, b: str) -> str:
            return f"({a}) AND ({b})" if a else b

        sampled = self._pull(src_conn, src_from, key, src_pred, limit=limit)
        key_tuples = [
            t for t in (tuple(r.get(k) for k in key) for r in sampled)
            if all(v is not None for v in t)
        ]
        if not key_tuples:
            return {"sampled": 0, "summary": {}, "diffs": [], "warnings": ["No sample keys found in the source."]}

        src_cols = list(dict.fromkeys([*key, *show_src]))
        tgt_key = [mappings.get(k, k) for k in key]
        tgt_cols = list(dict.fromkeys([*tgt_key, *[mappings.get(c, c) for c in show_src]]))
        src_rows = self._pull(src_conn, src_from, src_cols, both(src_pred, key_pred(key, key_tuples)))
        try:
            dst_raw = self._pull(dst_conn, dst_from, tgt_cols, both(dst_pred, key_pred(tgt_key, key_tuples)))
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the preview
            return {"sampled": len(src_rows), "summary": {}, "diffs": [],
                    "warnings": [f"Could not read the target for these keys: {exc}"]}

        # Rename target columns back to SOURCE names so compare_rows lines up by shared names.
        rev = {mappings.get(c, c): c for c in show_src}
        for k in key:
            rev[mappings.get(k, k)] = k
        dst_rows = [{rev.get(col, col): val for col, val in r.items()} for r in dst_raw]

        outcome = compare_rows(
            src_rows, dst_rows, key=list(key), columns=compare_cols, column_types=src_types,
            options=CompareOptions(fail_fast=False, bad_record_limit=min(limit, 50)),
        )
        diffs = [r for r in _outcome_detail(outcome) if r.get("bucket") != "match"][:50]
        return {"sampled": len(src_rows), "summary": _data_summary(outcome), "diffs": diffs, "warnings": []}

    def get_logs(self, ctx: TenantContext, job_id: str) -> list[dict]:
        """The timestamped execution trace of a compare's most recent run."""
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            job = self._load(s, ctx, job_id)
            return list(job.execution_log or [])

    def failure_trend(
        self, ctx: TenantContext, job_id: str, *, param_hash: str | None = None, limit: int = 50
    ) -> dict:
        """Failure-growth over time for a test case, scoped to ONE parameter set (so only
        like-for-like runs are compared). Returns the latest failed-record count, the change vs the
        previous run (abs + %), a direction, and the actual-value series for the hover chart.

        `param_hash` selects the set; omitted → the latest run's set (what the user is looking at).
        """
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            self._load(s, ctx, job_id)  # tenant-scope + existence check
            runs = s.scalars(
                select(CompareRun)
                .where(CompareRun.compare_job_id == job_id, CompareRun.group_id == ctx.group_id)
                .order_by(CompareRun.finished_at.asc())
            ).all()
            rows = [
                {"run_id": r.id, "ts": _iso(r.finished_at), "status": r.status,
                 "failed_count": r.failed_count, "param_hash": r.param_hash,
                 "variables": r.variables or {}}
                for r in runs
            ]
        if not rows:
            return {"job_id": job_id, "param_hash": None, **failure_trend([])}
        ph = param_hash or rows[-1]["param_hash"]
        # Only succeeded runs carry a real failed_count; keep the chosen parameter set, newest last.
        series = [r for r in rows if r["param_hash"] == ph and r["status"] == "succeeded"][-limit:]
        trend = failure_trend([
            {"ts": r["ts"], "failed_count": r["failed_count"], "status": r["status"]} for r in series
        ])
        return {"job_id": job_id, "param_hash": ph,
                "variables": series[-1]["variables"] if series else {}, **trend}

    def get_run_logs(self, ctx: TenantContext, run_id: str) -> list[dict]:
        """The execution trace of a SPECIFIC historical run (not just the latest)."""
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            run = s.scalar(
                select(CompareRun).where(
                    CompareRun.id == run_id, CompareRun.group_id == ctx.group_id
                )
            )
            if run is None:
                raise NotFoundError("run not found", details={"run_id": run_id})
            return list(run.execution_log or [])

    def list_jobs(self, ctx: TenantContext) -> list[dict]:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            jobs = s.scalars(
                select(CompareJob)
                .where(CompareJob.group_id == ctx.group_id)
                .order_by(CompareJob.created_at.desc())
            ).all()
            # One query for all results (keyed by job) instead of N per-job queries —
            # keeps list/batch views O(1) round-trips as a group accumulates test cases.
            summaries = self._summaries_for(s, [j.id for j in jobs])
            out = []
            for job in jobs:
                d = _job_dict(job)
                d["summary"] = summaries.get(job.id)
                out.append(d)
            self._attach_actor_names(s, out)
            return out

    def active_jobs(self, ctx: TenantContext) -> dict:
        """Everything in flight for the group — jobs that are RUNNING (executing now) or QUEUED
        (waiting for a worker slot). Powers the top-bar queue bell. `concurrency` is how many
        run in parallel, so the UI can show 'N running / limit · M waiting'.

        Only jobs actually submitted to the worker pool count — a created-but-never-run test case
        also carries status 'queued' in the model, so we intersect with the live-run set (ids the
        pool is working on) to keep stale drafts out of the bell."""
        ctx.require(Permission.READ)
        live = getattr(self, "active_runs", None)
        with self.store.session(ctx) as s:
            jobs = s.scalars(
                select(CompareJob)
                .where(
                    CompareJob.group_id == ctx.group_id,
                    CompareJob.status.in_(("running", "queued")),
                )
                # Running first, then oldest-queued first (FIFO) so the list reads like a queue.
                .order_by(CompareJob.status.desc(), CompareJob.created_at.asc())
            ).all()
            if live is not None:
                snapshot = set(live)  # copy — the worker pool mutates it concurrently
                jobs = [j for j in jobs if j.id in snapshot]
            out = []
            for job in jobs:
                d = {
                    "id": job.id,
                    "name": job.name or (job.batch_name or job.id[:8]),
                    "status": job.status,
                    "kind": job.kind,
                    "project_id": job.project_id,
                    "batch_id": job.batch_id,
                    "batch_name": job.batch_name,
                    "created_at": _iso(job.created_at),
                    "started_at": _iso(job.started_at),
                    "last_run_by": job.last_run_by,
                }
                out.append(d)
            self._attach_actor_names(s, out)
        running = sum(1 for d in out if d["status"] == "running")
        return {
            "jobs": out,
            "running": running,
            "queued": len(out) - running,
            "total": len(out),
            "concurrency": getattr(self, "_concurrency", None),
        }

    def list_runs(self, ctx: TenantContext, project_id: str | None = None, limit: int = 1000) -> list[dict]:
        """Execution HISTORY — every run, newest first (vs list_jobs, which is latest-per-test-case).
        A batch execution's pair-runs are grouped into one entry via their shared batch_run_id."""
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            q = select(CompareRun).where(CompareRun.group_id == ctx.group_id)
            if project_id:
                q = q.where(CompareRun.project_id == project_id)
            rows = s.scalars(q.order_by(CompareRun.created_at.desc())).all()[:limit]

            def rundict(r: CompareRun) -> dict:
                return {
                    "id": r.id,
                    "job_id": r.compare_job_id,
                    "name": r.name,
                    "kind": r.kind,
                    "status": r.status,
                    "match": (r.summary or {}).get("match"),
                    "summary": r.summary or {},
                    "started_at": _iso(r.started_at),
                    "finished_at": _iso(r.finished_at),
                    "last_run_by": r.run_by,
                    "has_log": bool(r.execution_log),
                }

            entries: list[dict] = []
            groups: dict[str, dict] = {}  # batch_run_id → entry (insertion order = newest first)
            for r in rows:
                rd = rundict(r)
                when = rd["finished_at"] or rd["started_at"] or _iso(r.created_at)
                if r.batch_run_id:
                    g = groups.get(r.batch_run_id)
                    if g is None:
                        g = {
                            "type": "batch", "id": r.batch_run_id, "batch_id": r.batch_id,
                            "name": r.batch_name or r.name, "kind": r.kind, "when": when,
                            "last_run_by": r.run_by, "runs": [],
                        }
                        groups[r.batch_run_id] = g
                        entries.append(g)
                    g["runs"].append(rd)
                    g["when"] = max(g["when"] or "", when or "")
                else:
                    entries.append({"type": "single", "id": r.id, "when": when,
                                    "last_run_by": r.run_by, "run": rd})

            # Aggregate each batch run's pairs into a headline status + counts.
            for g in entries:
                if g["type"] != "batch":
                    continue
                runs = g["runs"]
                g["total"] = len(runs)
                g["succeeded"] = sum(1 for x in runs if x["status"] == "succeeded")
                g["failed"] = sum(1 for x in runs if x["status"] == "failed")
                g["cancelled"] = sum(1 for x in runs if x["status"] == "cancelled")
                g["matched"] = sum(1 for x in runs if x["match"] is True)
                g["differed"] = sum(1 for x in runs if x["match"] is False)
                if g["cancelled"] and g["succeeded"] == 0 and g["failed"] == 0:
                    g["status"] = "cancelled"
                elif g["failed"] == 0 and g["cancelled"] == 0:
                    g["status"] = "succeeded" if g["differed"] == 0 else "differences"
                elif g["succeeded"] == 0 and g["cancelled"] == 0:
                    g["status"] = "failed"
                else:
                    g["status"] = "partial"

            # Resolve run_by ids → names in one pass (nested batch runs too).
            flat = list(entries)
            for g in entries:
                if g["type"] == "batch":
                    flat.extend(g["runs"])
                else:
                    flat.append(g["run"])
            self._attach_actor_names(s, flat)
            return entries

    @staticmethod
    def _attach_actor_names(s, dicts: list[dict]) -> None:  # noqa: ANN001
        """Resolve `created_by`/`last_run_by` user ids → display names in one query, adding
        `created_by_name` / `last_run_by_name` to each dict (nested batch `jobs` too)."""
        def collect(ds: list[dict], ids: set[str]) -> None:
            for d in ds:
                for k in ("created_by", "last_run_by"):
                    if d.get(k):
                        ids.add(d[k])
                if isinstance(d.get("jobs"), list):
                    collect(d["jobs"], ids)

        ids: set[str] = set()
        collect(dicts, ids)
        if not ids:
            return
        names: dict[str, str] = {}
        for u in s.scalars(select(User).where(User.id.in_(ids))):
            names[u.id] = u.display_name or u.email or u.id[:8]

        def apply(ds: list[dict]) -> None:
            for d in ds:
                d["created_by_name"] = names.get(d.get("created_by")) if d.get("created_by") else None
                d["last_run_by_name"] = names.get(d.get("last_run_by")) if d.get("last_run_by") else None
                if isinstance(d.get("jobs"), list):
                    apply(d["jobs"])

        apply(dicts)

    @staticmethod
    def _summaries_for(s, job_ids: list[str]) -> dict[str, dict]:  # noqa: ANN001
        """{job_id: summary} for many jobs in a single query (de-N+1)."""
        if not job_ids:
            return {}
        rows = s.scalars(
            select(CompareResult).where(CompareResult.compare_job_id.in_(job_ids))
        ).all()
        return {r.compare_job_id: r.summary for r in rows}

    # ----------------------------- batch (up to 100 table pairs) -----------------------------
    MAX_BATCH = 100

    def run_batch(
        self,
        ctx: TenantContext,
        *,
        kind: str,
        name: str,
        source: dict,
        target: dict,
        tables: list[str] | None = None,
        table_pairs: list[dict] | None = None,
        options: dict | None = None,
        variables: dict | None = None,
        project_id: str | None = None,
    ) -> dict:
        """Create and run one compare per pair, continue-on-error → partial.

        A pair is one of two forms (see `_resolve_pairs`): same/differing table names on
        the shared batch connections, or fully independent per-side specs (table/SQL/file)
        from the dataset-mapping UI."""
        ctx.require(Permission.WRITE)
        resolved = self._resolve_pairs(tables, table_pairs, source, target)

        batch_id = new_id()
        batch_run_id = new_id()  # groups this execution's pair-runs in the history
        run_vars = {**(variables or {}), "__batch_run_id__": batch_run_id}
        jobs: list[dict] = []
        for src_spec, tgt_spec, label, pair_opts in resolved:
            job = self.create_job(
                ctx,
                kind=kind,
                name=f"{name}: {label}",
                source=src_spec,
                target=tgt_spec,
                options={**(options or {}), **pair_opts},
                batch_id=batch_id,
                batch_name=name,
                project_id=project_id,
            )
            try:
                self.run(ctx, job["id"], run_vars)
            except Exception as exc:  # noqa: BLE001 - one bad pair never sinks the batch
                with self.store.session(ctx) as s:
                    j = self._load(s, ctx, job["id"])
                    j.status = "failed"
                    j.finished_at = utcnow()
                    j.error = str(exc)
                    self._record_run(s, j, {}, batch_run_id)
            jobs.append(self.get_job(ctx, job["id"]))
        return _batch_summary(batch_id, name, kind, jobs)

    def _resolve_pairs(
        self,
        tables: list[str] | None,
        table_pairs: list[dict] | None,
        source: dict,
        target: dict,
    ) -> list[tuple[dict, dict, str, dict]]:
        """Normalise a batch request into a list of (source_spec, target_spec, label, pair_opts).

        Accepted inputs, in precedence order:
          - `table_pairs` with full specs: [{"source": {...}, "target": {...}, "label"?,
            "options"?}] — each side is an independent table/SQL/file spec; the batch-level
            connection is a default. A pair may carry its own `options` (e.g. per-pair
            `column_mappings`/`key`) merged over the batch options. Produced by the mapping UI.
          - `table_pairs` with names: [{"source_table": "ORD", "target_table": "ORDERS"}] —
            table names merged onto the shared batch connection + schema.
          - `tables`: ["ORDERS", ...] — same name on both sides.
        """
        pairs = table_pairs or [{"source_table": t, "target_table": t} for t in (tables or [])]
        if not pairs:
            raise ValidationError("batch needs 'tables' or 'table_pairs'")
        if len(pairs) > self.MAX_BATCH:
            raise ValidationError(
                f"batch is limited to {self.MAX_BATCH} pairs", details={"got": len(pairs)}
            )
        out: list[tuple[dict, dict, str, dict]] = []
        for pair in pairs:
            pair_opts = pair.get("options") if isinstance(pair.get("options"), dict) else {}
            if isinstance(pair.get("source"), dict) and isinstance(pair.get("target"), dict):
                src_spec = {**source, **pair["source"]}
                tgt_spec = {**target, **pair["target"]}
                # A SQL side must not carry a leftover table/schema from the batch default.
                if "sql" in pair["source"]:
                    src_spec = {"connection_id": src_spec.get("connection_id"), "sql": pair["source"]["sql"]}
                if "sql" in pair["target"]:
                    tgt_spec = {"connection_id": tgt_spec.get("connection_id"), "sql": pair["target"]["sql"]}
                label = pair.get("label") or f"{_spec_label(src_spec)}→{_spec_label(tgt_spec)}"
            else:
                st, tt = pair.get("source_table"), pair.get("target_table")
                if not st or not tt:
                    raise ValidationError(
                        "each pair needs source_table+target_table or source+target specs",
                        details={"pair": pair},
                    )
                src_spec = {**source, "table": st}
                tgt_spec = {**target, "table": tt}
                label = st if st == tt else f"{st}→{tt}"
            out.append((src_spec, tgt_spec, label, pair_opts))
        return out

    def create_batch(
        self,
        ctx: TenantContext,
        *,
        kind: str,
        name: str,
        source: dict,
        target: dict,
        tables: list[str] | None = None,
        table_pairs: list[dict] | None = None,
        options: dict | None = None,
        project_id: str | None = None,
    ) -> tuple[str, list[str]]:
        """Create (but do NOT run) one compare per table pair. Returns (batch_id, job_ids)
        so the caller can enqueue each run on the worker tier (async, off the API path)."""
        ctx.require(Permission.WRITE)
        resolved = self._resolve_pairs(tables, table_pairs, source, target)
        batch_id = new_id()
        job_ids = []
        for src_spec, tgt_spec, label, pair_opts in resolved:
            job = self.create_job(
                ctx,
                kind=kind,
                name=f"{name}: {label}",
                source=src_spec,
                target=tgt_spec,
                options={**(options or {}), **pair_opts},
                batch_id=batch_id,
                batch_name=name,
                project_id=project_id,
            )
            job_ids.append(job["id"])
        return batch_id, job_ids

    def requeue_batch(self, ctx: TenantContext, batch_id: str) -> list[tuple[str, dict]]:
        """Reset every job in a batch to 'queued' (clearing the prior result state) so a rerun
        shows as in-progress immediately. Returns (job_id, stored_variables) for the caller to
        enqueue on the worker tier."""
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            rows = s.scalars(
                select(CompareJob).where(
                    CompareJob.group_id == ctx.group_id, CompareJob.batch_id == batch_id
                )
            ).all()
            if not rows:
                raise NotFoundError("batch not found", details={"batch_id": batch_id})
            out = []
            for j in rows:
                j.status = "queued"
                j.error = None
                j.finished_at = None
                out.append((j.id, dict(j.variables or {})))
            return out

    def mark_failed(self, ctx: TenantContext, job_id: str, error: str) -> None:
        with self.store.session(ctx) as s:
            job = self._load(s, ctx, job_id)
            job.status = "failed"
            job.finished_at = utcnow()
            job.error = error

    # ----------------------------- recurring schedules -----------------------------
    def set_schedule(self, ctx: TenantContext, job_id: str, cron: str | None) -> dict:
        """Set (or clear, with None/"") a recurring cron schedule on a test case. For a batch pair,
        the schedule applies to the WHOLE batch (set on every pair) so the batch re-runs as a unit."""
        ctx.require(Permission.WRITE)
        cron = (cron or "").strip() or None
        if cron is not None and not cron_is_valid(cron):
            raise ValidationError("invalid cron expression", details={"schedule_cron": cron})
        with self.store.session(ctx) as s:
            job = self._load(s, ctx, job_id)
            targets = [job]
            if job.batch_id:  # schedule the batch as a unit — every pair carries the same cron
                targets = s.scalars(
                    select(CompareJob).where(
                        CompareJob.group_id == ctx.group_id, CompareJob.batch_id == job.batch_id
                    )
                ).all()
            for t in targets:
                t.schedule_cron = cron
            audit.record(s, ctx, "compare.schedule", entity_type="compare_job", entity_id=job.id)
        return self.get_job(ctx, job_id)

    def due_schedules(self, ctx: TenantContext, now) -> list[tuple[str, str]]:  # noqa: ANN001
        """Return the test cases whose schedule has come due since their last run, as
        (target_kind, id) where target_kind is 'single' (job id) or 'batch' (batch id). A batch is
        evaluated ONCE against the latest run across its pairs, so it fires as a unit."""
        from dvp.core.cron import previous_due

        with self.store.session(ctx) as s:
            jobs = s.scalars(
                select(CompareJob).where(
                    CompareJob.group_id == ctx.group_id,
                    CompareJob.schedule_cron.is_not(None),
                )
            ).all()
            if not jobs:
                return []
            # Last execution time per test case: max finished_at over a batch's pairs, else the job's.
            last_by_single: dict[str, object] = {}
            last_by_batch: dict[str, object] = {}
            cron_by_batch: dict[str, str] = {}
            singles: dict[str, str] = {}
            for j in jobs:
                fin = j.finished_at
                if j.batch_id:
                    cron_by_batch[j.batch_id] = j.schedule_cron or ""
                    cur = last_by_batch.get(j.batch_id)
                    if fin and (cur is None or fin > cur):
                        last_by_batch[j.batch_id] = fin
                    last_by_batch.setdefault(j.batch_id, None)
                else:
                    singles[j.id] = j.schedule_cron or ""
                    last_by_single[j.id] = fin

        def _is_due(cron: str, last) -> bool:  # noqa: ANN001
            slot = previous_due(cron, now)
            if slot is None:
                return False
            if last is not None and getattr(last, "tzinfo", None) is None:
                from datetime import UTC

                last = last.replace(tzinfo=UTC)
            return last is None or last < slot

        due: list[tuple[str, str]] = []
        for jid, cron in singles.items():
            if _is_due(cron, last_by_single.get(jid)):
                due.append(("single", jid))
        for bid, cron in cron_by_batch.items():
            if _is_due(cron, last_by_batch.get(bid)):
                due.append(("batch", bid))
        return due

    def _persist_key_detection(self, ctx: TenantContext, job_id, detected_key, status) -> None:  # noqa: ANN001
        """Record the run-time KEY-detection outcome on the test case (best-effort — bookkeeping must
        never fail the run). Persisted in its own session so it survives even when the run then fails
        because no key could be found — the mapping UI still shows 'not detected on <date>'."""
        if not job_id:
            return
        try:
            with self.store.session(ctx) as s:
                job = s.get(CompareJob, job_id)
                if job is not None and job.group_id == ctx.group_id:
                    job.detected_key = list(detected_key or [])
                    job.key_status = status
                    job.key_detected_at = utcnow()
        except Exception:  # noqa: BLE001 - never fail a run on key-detection bookkeeping
            log.warning(
                "key_detection_bookkeeping_failed",
                job_id=job_id,
                group_id=ctx.group_id,
                exc_info=True,
            )

    @staticmethod
    def _record_run(s, job, summary, batch_run_id=None, variables=None) -> None:  # noqa: ANN001
        """Append a history snapshot of this execution so the Executions view can show every run,
        not just the latest. Also captures the run's params (`variables` + their `param_hash`) and a
        normalized `failed_count` so the UI can trend failure growth per parameter set. Best-effort —
        a history write must never fail the run itself."""
        try:
            vars_ = dict(variables or {})
            s.add(
                CompareRun(
                    org_id=job.org_id,
                    group_id=job.group_id,
                    compare_job_id=job.id,
                    project_id=job.project_id,
                    batch_id=job.batch_id,
                    batch_name=job.batch_name,
                    batch_run_id=batch_run_id,
                    name=job.name,
                    kind=job.kind,
                    status=job.status,
                    summary=summary or {},
                    variables=vars_,
                    param_hash=param_hash(vars_),
                    failed_count=failed_count_from_summary(job.kind, summary),
                    error=job.error,
                    execution_log=list(job.execution_log or []),
                    started_at=job.started_at,
                    finished_at=job.finished_at,
                    run_by=job.last_run_by,
                )
            )
        except Exception:  # noqa: BLE001 - history is best-effort
            log.warning(
                "compare_run_history_write_failed",
                job_id=getattr(job, "id", None),
                group_id=getattr(job, "group_id", None),
                exc_info=True,
            )

    def _progress_cb(self, ctx: TenantContext, job_id: str | None):  # noqa: ANN202
        """A callback the streaming engine calls after each window to publish live progress on the
        job ({pct, processed, total}), so the UI can show how far a long run has got. Throttled to
        meaningful changes and fully best-effort — a progress write must never fail the run."""
        if not job_id:
            return None
        last_pct = {"v": -1}

        def _update(processed: int, total: int) -> None:
            pct = int(100 * processed / total) if total else 0
            if pct <= last_pct["v"] and processed < total:
                return  # only write on a percentage change (or the final tick)
            last_pct["v"] = pct
            try:
                with self.store.session(ctx) as s:
                    job = s.get(CompareJob, job_id)
                    if job is not None and job.group_id == ctx.group_id:
                        job.progress = {"pct": min(100, pct), "processed": processed, "total": total}
                        job.heartbeat_at = utcnow()
            except Exception:  # noqa: BLE001 - progress is best-effort
                log.warning(
                    "compare_progress_write_failed", job_id=job_id, group_id=ctx.group_id, exc_info=True
                )

        return _update

    def _clear_progress(self, ctx: TenantContext, job_id: str | None) -> None:
        if not job_id:
            return
        try:
            with self.store.session(ctx) as s:
                job = s.get(CompareJob, job_id)
                if job is not None and job.group_id == ctx.group_id:
                    job.progress = None
        except Exception:  # noqa: BLE001
            pass

    def _register_cancel(self, job_id, *conns) -> None:  # noqa: ANN002
        """Register the connectors a running job queries through, so a cancel can abort their
        in-flight query."""
        reg = getattr(self, "cancel_registry", None)
        if reg and job_id:
            reg.register(job_id, *conns)

    def _should_cancel(self, job_id):  # noqa: ANN001, ANN201
        reg = getattr(self, "cancel_registry", None)
        if reg and job_id:
            return lambda: reg.is_cancelled(job_id)
        return None

    def _mark_cancelled(self, ctx: TenantContext, job_id: str, rlog=None, batch_run_id=None) -> dict:  # noqa: ANN001
        with self.store.session(ctx) as s:
            job = self._load(s, ctx, job_id)
            # Don't clobber a terminal state that landed first (race with a just-finished run).
            if job.status in ("queued", "running"):
                job.status = "cancelled"
                job.finished_at = utcnow()
                job.error = "cancelled by user"
                if rlog is not None:
                    job.execution_log = rlog.lines
                self._record_run(s, job, {}, batch_run_id)
        return self.get_job(ctx, job_id)

    def cancel_job(self, ctx: TenantContext, job_id: str) -> dict:
        """Request cancellation of a running/queued compare: flag it (the engine stops between
        queries) and abort its in-flight warehouse query. Marks it cancelled immediately."""
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            self._load(s, ctx, job_id)  # 404s unless in the caller's group
        if self.cancel_registry:
            self.cancel_registry.request(job_id)
        return self._mark_cancelled(ctx, job_id)

    def cancel_batch(self, ctx: TenantContext, batch_id: str) -> dict:
        """Cancel every running/queued pair in a batch."""
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            rows = s.scalars(
                select(CompareJob).where(
                    CompareJob.group_id == ctx.group_id, CompareJob.batch_id == batch_id
                )
            ).all()
            if not rows:
                raise NotFoundError("batch not found", details={"batch_id": batch_id})
            ids = [j.id for j in rows]
        cancelled = 0
        for jid in ids:
            if self.cancel_registry:
                self.cancel_registry.request(jid)
            with self.store.session(ctx) as s:
                job = self._load(s, ctx, jid)
                if job.status in ("queued", "running"):
                    job.status = "cancelled"
                    job.finished_at = utcnow()
                    job.error = "cancelled by user"
                    cancelled += 1
        return {"batch_id": batch_id, "cancelled": cancelled}

    def mark_queued(self, ctx: TenantContext, job_id: str) -> tuple[float, int]:
        """Flag a job as QUEUED the moment it's handed to the worker tier — synchronously, before
        a worker picks it up — so the queue bell shows it as waiting even while all slots are busy.
        Clears the previous run's outcome so a re-run reads as fresh work, not stale results.
        Returns the job's (memory_gb, vcpus) resource needs for the scheduler."""
        with self.store.session(ctx) as s:
            job = self._load(s, ctx, job_id)
            job.status = "queued"
            job.started_at = None
            job.finished_at = None
            job.error = None
            mem, cpu = self._estimate_resources(s, ctx, job)
        return mem, cpu

    def _estimate_resources(self, s, ctx: TenantContext, job) -> tuple[float, int]:  # noqa: ANN001
        """The (memory_gb, vcpus) a run will need, so the scheduler packs the pool correctly.

        A PUSHDOWN run (Snowflake hash / in-database recursive) computes inside the warehouse and
        holds only a small sampled/aggregated footprint locally — so it must NOT reserve the full
        in-memory budget, or we'd needlessly serialize jobs that could all run at once. We charge
        the reduced footprint ONLY when the run is CERTAIN to push down (never under-provision an
        in-memory run): both sides Snowflake + exact (tolerance 0) + engine not forced to
        inmemory/spark ⇒ _run_data always routes to recursive/hash pushdown, never in-memory."""
        opts = job.options or {}
        # vCPUs default to 1; a Spark job can ask for more.
        cpu = int(opts.get("vcpus") or 1)
        # Per-test in-memory budget; default a light 1 GB so untagged jobs still pack sensibly.
        budget = float(opts.get("memory_gb") or 1.0)
        requested = str(opts.get("engine", "auto")).lower()
        tolerance = float(opts.get("tolerance") or 0.0)
        if requested not in ("inmemory", "spark") and tolerance == 0.0:
            src_t = self._conn_source_type(s, (job.source or {}).get("connection_id"))
            dst_t = self._conn_source_type(s, (job.target or {}).get("connection_id"))
            if src_t == "snowflake" and dst_t == "snowflake":
                return _PUSHDOWN_MEMORY_GB, cpu
        return budget, cpu

    @staticmethod
    def _conn_source_type(s, connection_id: str | None) -> str | None:  # noqa: ANN001
        if not connection_id:
            return None
        c = s.get(Connection, connection_id)
        return c.source_type if c is not None else None

    def run_safe(self, ctx: TenantContext, job_id: str, variables: dict | None = None) -> None:
        """Worker entrypoint — run a job and guarantee the failure is recorded, never raised
        out of the worker (so one bad run can't crash the pool or vanish silently)."""
        try:
            self.run(ctx, job_id, variables)
        except Exception as exc:  # noqa: BLE001 - worker boundary
            self.mark_failed(ctx, job_id, str(exc))
        finally:
            runs = getattr(self, "active_runs", None)
            if runs is not None:
                runs.discard(job_id)  # no longer in flight — drop from the queue bell

    def list_batches(self, ctx: TenantContext) -> list[dict]:
        ctx.require(Permission.READ)
        by_batch: dict[str, list[dict]] = {}
        for j in self.list_jobs(ctx):
            if j.get("batch_id"):
                by_batch.setdefault(j["batch_id"], []).append(j)
        return [
            _batch_summary(bid, js[0].get("batch_name") or bid, js[0]["kind"], js)
            for bid, js in by_batch.items()
        ]

    def get_batch(self, ctx: TenantContext, batch_id: str) -> dict:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            # Scope by group AND batch_id directly — never scan the group's whole job set.
            rows = s.scalars(
                select(CompareJob)
                .where(CompareJob.group_id == ctx.group_id, CompareJob.batch_id == batch_id)
                .order_by(CompareJob.created_at.desc())
            ).all()
            if not rows:
                raise NotFoundError("batch not found", details={"batch_id": batch_id})
            summaries = self._summaries_for(s, [j.id for j in rows])
            jobs = [{**_job_dict(j), "summary": summaries.get(j.id)} for j in rows]
            self._attach_actor_names(s, jobs)
        out = _batch_summary(batch_id, jobs[0].get("batch_name") or batch_id, jobs[0]["kind"], jobs)
        out["jobs"] = jobs
        return out

    # ----------------------------- delete (cascade to results + artifacts) -----------------------------
    def delete_job(self, ctx: TenantContext, job_id: str) -> dict:
        """Delete a test case and everything it owns: its result rows AND the detail
        artifacts in the ResultStore (so stored cell values never outlive the test case).
        Group-scoped; artifact cleanup failures surface as warnings, never swallowed."""
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            job = self._load(s, ctx, job_id)  # raises unless the job is in the caller's group
            refs = self._purge_results(s, [job.id])
            s.delete(job)
            audit.record(s, ctx, "compare.delete", entity_type="compare_job", entity_id=job_id)
        # Artifacts are deleted AFTER the metadata txn commits, so a storage error can't
        # roll back (and thus resurrect) a test case the user asked to remove.
        warnings = self._delete_artifacts(ctx, refs)
        return {"deleted": job_id, "artifacts_deleted": len(refs) - len(warnings), "warnings": warnings}

    def delete_batch(self, ctx: TenantContext, batch_id: str) -> dict:
        """Delete every test case in a batch, with the same result + artifact cleanup."""
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            jobs = s.scalars(
                select(CompareJob).where(
                    CompareJob.group_id == ctx.group_id, CompareJob.batch_id == batch_id
                )
            ).all()
            if not jobs:
                raise NotFoundError("batch not found", details={"batch_id": batch_id})
            refs = self._purge_results(s, [j.id for j in jobs])
            for job in jobs:
                s.delete(job)
            audit.record(
                s,
                ctx,
                "compare.delete_batch",
                entity_type="compare_batch",
                entity_id=batch_id,
                detail={"jobs": len(jobs)},
            )
        warnings = self._delete_artifacts(ctx, refs)
        return {
            "deleted_batch": batch_id,
            "deleted_jobs": len(jobs),
            "artifacts_deleted": len(refs) - len(warnings),
            "warnings": warnings,
        }

    @staticmethod
    def _purge_results(s, job_ids: list[str]) -> list[str]:  # noqa: ANN001
        """Delete the CompareResult rows for these jobs (single query) and return their
        detail_refs so the caller can delete the backing artifacts. Flushes so the child
        rows are gone before the parent jobs are deleted (no ORM relationship declares the
        dependency, so the FK order must be forced explicitly)."""
        results = s.scalars(
            select(CompareResult).where(CompareResult.compare_job_id.in_(job_ids))
        ).all()
        refs = [r.detail_ref for r in results if r.detail_ref]
        for r in results:
            s.delete(r)
        # Run-history snapshots go too, so a deleted test case leaves no orphan executions.
        for run in s.scalars(
            select(CompareRun).where(CompareRun.compare_job_id.in_(job_ids))
        ).all():
            s.delete(run)
        s.flush()
        return refs

    def _delete_artifacts(self, ctx: TenantContext, refs: list[str]) -> list[dict]:
        warnings: list[dict] = []
        for ref in refs:
            try:
                self._reader(ctx, ref).delete(ctx, ref)
            except Exception as exc:  # noqa: BLE001 - one bad artifact shouldn't block the rest
                warnings.append(
                    {"code": "artifact_delete_failed", "message": str(exc), "details": {"ref": ref}}
                )
        return warnings

    def run_inmemory(
        self,
        ctx: TenantContext,
        job_id: str,
        *,
        src_rows: list[dict],
        dst_rows: list[dict],
        key: list[str],
        columns: list[str],
        column_types: dict[str, str] | None = None,
        variables: dict | None = None,
    ) -> dict:
        """Execute a data compare over in-memory datasets (test/desktop path).

        Summary → metadata DB; cell-level detail → ResultStore (D9 residency). Also appends a
        CompareRun history row (with the run's params) so the failure-growth trend works here too."""
        ctx.require(Permission.WRITE)
        outcome = compare_rows(
            src_rows,
            dst_rows,
            key=key,
            columns=columns,
            column_types=column_types,
            options=CompareOptions(),
        )
        detail_ref = self._writer(ctx).write_detail(
            ctx, f"compare_{job_id}", _outcome_detail(outcome)
        )
        summary = _data_summary(outcome)

        with self.store.session(ctx) as s:
            job = self._load(s, ctx, job_id)
            job.status = "succeeded"
            job.last_run_by = ctx.user_id  # who triggered this execution
            job.finished_at = utcnow()
            s.add(
                CompareResult(
                    compare_job_id=job.id,
                    summary=summary,
                    detected_key=key,
                    warnings=[],
                    detail_ref=detail_ref,
                    variables=dict(variables or {}),
                )
            )
            self._record_run(s, job, summary, variables=variables)
            audit.record(s, ctx, "compare.run", entity_type="compare_job", entity_id=job.id)
        return self.get_job(ctx, job_id)

    # ----------------------------- live execution (V1 + V3-local) -----------------------------
    def variables_for(self, ctx: TenantContext, job_id: str) -> list[str]:
        """Variable names referenced by a test case's definition."""
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            job = self._load(s, ctx, job_id)
            defn = [dict(job.source), dict(job.target), dict(job.options)]
        return sorted(extract_variables(defn))

    def run(self, ctx: TenantContext, job_id: str, variables: dict | None = None) -> dict:
        """Execute a queued compare against its live connectors, by kind.

        `variables` supplies values for ``{{var}}`` placeholders in the definition; the
        resolved values are recorded on the result."""
        ctx.require(Permission.WRITE)
        variables = dict(variables or {})
        # Reserved: groups every pair-run of one batch execution into a single "batch run" in the
        # history. Popped so it never leaks into {{var}} substitution or the stored variables.
        batch_run_id = variables.pop("__batch_run_id__", None)
        rlog = RunLog()
        with self.store.session(ctx) as s:
            job = self._load(s, ctx, job_id)
            job.status = "running"  # so async pollers see progress before completion
            job.last_run_by = ctx.user_id  # who triggered this execution
            job.started_at = utcnow()
            job.progress = None  # clear any stale progress from a prior run
            kind = job.kind
            source, target, options = dict(job.source), dict(job.target), dict(job.options)
            # Project-level {{variables}} are the base layer — inherited by every test case in the
            # project — overridden by anything passed to this run (job-stored or run-time values).
            project_vars: dict = {}
            if job.project_id:
                proj = s.get(Project, job.project_id)
                if proj is not None:
                    project_vars = dict(proj.variables or {})
        variables = {**project_vars, **variables}
        rlog.log(f"Execution started — {kind} compare (by {ctx.user_id})")
        # If a cancel was requested before we even started, honour it immediately.
        if self.cancel_registry and self.cancel_registry.is_cancelled(job_id):
            rlog.log("Cancelled before start", "warn")
            return self._mark_cancelled(ctx, job_id, rlog, batch_run_id)
        try:
            # Resolve {{var}} placeholders; fail clearly if any are unsupplied. Kept INSIDE the try
            # (after status=running) so the handler below resolves the job rather than stranding it.
            missing = missing_variables([source, target, options], variables)
            if missing:
                raise ValidationError(
                    f"missing runtime variables: {', '.join(missing)}", details={"missing": missing}
                )
            source = substitute(source, variables)
            target = substitute(target, variables)
            options = substitute(options, variables)
            # Vet user-supplied SQL fragments AFTER substitution — a malicious value could otherwise
            # be smuggled in through a {{variable}} that looked harmless in the stored definition.
            validate_compare_side(source, side="source")
            validate_compare_side(target, side="target")
            if kind == "count":
                summary, detail, key, warnings = self._run_count(ctx, source, target, options, job_id, rlog)
            elif kind == "schema":
                summary, detail, key, warnings = self._run_schema(ctx, source, target, options, job_id, rlog)
            elif kind == "data":
                summary, detail, key, warnings = self._run_data(ctx, source, target, options, job_id, rlog)
            else:
                raise ValidationError(f"unknown compare kind: {kind}")
        except CompareCancelled:
            rlog.log("Cancelled by user — engine stopped, in-flight query aborted", "warn")
            return self._mark_cancelled(ctx, job_id, rlog, batch_run_id)
        except (NotFoundError, ValidationError) as exc:
            # User-facing input errors (bad table/column, missing variable, unknown kind). Resolve
            # the job — mark failed + record the run — so a SYNCHRONOUS :run that surfaces the error
            # to the caller never leaves the job stuck "running". Then re-raise for the 400/404.
            rlog.log(f"FAILED: {exc}", "error")
            with self.store.session(ctx) as s:
                job = self._load(s, ctx, job_id)
                job.status = "failed"
                job.finished_at = utcnow()
                job.error = str(exc)
                job.execution_log = rlog.lines
                self._record_run(s, job, {}, batch_run_id, variables=variables)
            self._link_compare(ctx, job_id)
            raise
        except Exception as exc:  # noqa: BLE001 - record failure, don't swallow
            # A query aborted by our own cancel (in-flight) surfaces as a DB error — report it
            # as cancelled, not failed.
            if self.cancel_registry and self.cancel_registry.is_cancelled(job_id):
                rlog.log("Cancelled by user (query aborted)", "warn")
                return self._mark_cancelled(ctx, job_id, rlog, batch_run_id)
            rlog.log(f"FAILED: {exc}", "error")
            with self.store.session(ctx) as s:
                job = self._load(s, ctx, job_id)
                job.status = "failed"
                job.finished_at = utcnow()
                job.error = str(exc)
                job.execution_log = rlog.lines
                self._record_run(s, job, {}, batch_run_id, variables=variables)
            self._link_compare(ctx, job_id)
            return self.get_job(ctx, job_id)
        finally:
            if self.cancel_registry:
                self.cancel_registry.finish(job_id)

        rlog.notes([w.get("message") for w in warnings if w.get("message")], "warn")
        # The OUTCOME (match verdict, row counts, diffs) belongs to the result and is shown in
        # its own panel — the log stays a purely operational trace of what the engine did.
        rlog.log(f"Execution finished — engine={summary.get('engine', kind)}")
        detail_ref = (
            self._writer(ctx).write_detail(ctx, f"compare_{job_id}", detail) if detail else None
        )
        decided = bool(summary.get("decision_required"))
        with self.store.session(ctx) as s:
            job = self._load(s, ctx, job_id)
            job.status = "needs_decision" if decided else "succeeded"
            job.finished_at = utcnow()
            job.execution_log = rlog.lines
            # Replace any prior result so re-running a compare is idempotent.
            for old in s.scalars(
                select(CompareResult).where(CompareResult.compare_job_id == job.id)
            ).all():
                s.delete(old)
            s.flush()
            s.add(
                CompareResult(
                    compare_job_id=job.id,
                    summary=summary,
                    detected_key=key,
                    warnings=warnings,
                    detail_ref=detail_ref,
                    variables=variables,
                )
            )
            self._record_run(s, job, summary, batch_run_id, variables=variables)
            audit.record(
                s,
                ctx,
                "compare.run",
                entity_type="compare_job",
                entity_id=job.id,
                detail={
                    "kind": job.kind,
                    "status": job.status,
                    "sample_location": _sample_location(detail_ref),
                },
            )
        # A decision-required outcome has no verdict yet — don't drive incident/regression alerting.
        if not decided:
            self._link_compare(ctx, job_id)
        return self.get_job(ctx, job_id)

    def _link_compare(self, ctx: TenantContext, job_id: str) -> None:
        """Attach a finished compare to its incident and drive REGRESSION alerting.

        * Opened-on-an-incident compare → just attach the result (no alerting here).
        * Clean pass → auto-resolve any open incident for this compare (recovery closes the loop).
        * Differences → open/update one incident (dedup per test case). If the PREVIOUS run was a
          clean pass, it's a regression: escalate to critical and say so.
        * Execution failure after a passing run → open an incident (the test case stopped running).
          A compare that was already failing/differing just stays as-is (no new noise).
        """
        from dvp.observability.incidents import IncidentService

        job = self.get_job(ctx, job_id)
        options = job.get("options") or {}
        summary = (job.get("result") or {}).get("summary") or {}
        match = summary.get("match")
        status = job.get("status")
        payload = {
            "job_id": job["id"],
            "name": job.get("name"),
            "kind": job.get("kind"),
            "status": status,
            "match": match,
            "summary": summary,
        }
        inc = IncidentService(self.store)
        incident_id = options.get("incident_id")
        if incident_id:
            try:
                inc.attach_compare(ctx, str(incident_id), payload)
            except NotFoundError:
                pass
            return

        dedup_key = f"compare:{job['id']}"
        target = job.get("target") or {}
        table = _side_table_ref(target)
        short = (table or "table").split(".")[-1]
        label = job.get("name") or short
        prev = self._previous_verdict(ctx, job_id)  # verdict of the run BEFORE this one

        # Recovery: a clean pass clears any open incident for this compare.
        if status == "succeeded" and match is True:
            inc.resolve_by_key(ctx, dedup_key)
            return

        monitor_id = self._monitor_for_table(ctx, target.get("connection_id"), table)

        # Differences → open/update. A pass→diff transition is a true regression (escalate).
        if status == "succeeded" and match is False:
            regressed = prev == "pass"
            text = (
                f"Compare {label} regressed — now finds differences on {short}"
                if regressed
                else f"Compare {label} found differences on {short}"
            )
            inc.open_or_update(
                ctx,
                monitor_id=monitor_id,
                dedup_key=dedup_key,
                kind="compare",
                dimension="compare",
                severity="critical" if regressed else "warning",
                summary=text,
                table_ref=table,
                evidence={"compare": payload, "job_id": job["id"], "regression": regressed},
            )
            return

        # Execution regression: a compare that used to pass now fails to even run.
        if status == "failed" and prev == "pass":
            reason = (job.get("error") or "execution failed")[:140]
            inc.open_or_update(
                ctx,
                monitor_id=monitor_id,
                dedup_key=dedup_key,
                kind="compare",
                dimension="compare",
                severity="warning",
                summary=f"Compare {label} stopped running — {reason}",
                table_ref=table,
                evidence={"job_id": job["id"], "error": job.get("error"), "regression": True},
            )

    def _previous_verdict(self, ctx: TenantContext, job_id: str) -> str | None:
        """Verdict of the run BEFORE the current one for this test case, as 'pass' | 'diff' | 'fail'
        (None if there's no prior run, or it was cancelled). The current run is already recorded in
        history by the time this runs, so the 'previous' run is the 2nd-most-recent snapshot."""
        with self.store.session(ctx) as s:
            rows = s.scalars(
                select(CompareRun)
                .where(
                    CompareRun.group_id == ctx.group_id,
                    CompareRun.compare_job_id == job_id,
                )
                .order_by(CompareRun.created_at.desc(), CompareRun.id.desc())
                .limit(2)
            ).all()
        if len(rows) < 2:
            return None
        return _run_verdict(rows[1].status, rows[1].summary or {})

    def _monitor_for_table(
        self, ctx: TenantContext, connection_id: str | None, table_ref: str | None
    ) -> str | None:
        from dvp.db.models import Monitor

        if not connection_id:
            return None
        short = (table_ref or "").split(".")[-1].upper()
        with self.store.session(ctx) as s:
            mons = s.scalars(
                select(Monitor).where(
                    Monitor.group_id == ctx.group_id,
                    Monitor.connection_id == connection_id,
                    Monitor.status != "deleted",
                )
            ).all()
        for mon in mons:
            names = [
                str(t).split(".")[-1].upper()
                for t in ((mon.scope or {}).get("tables") or [])
            ]
            if short and short in names:
                return mon.id
        return mons[0].id if mons else None

    def _run_count(self, ctx, source, target, options, job_id=None, rlog=None):  # noqa: ANN001
        _log = rlog.log if rlog is not None else (lambda *a, **k: None)
        sc, sfrom, stref = self._side(ctx, source)
        dc, dfrom, dtref = self._side(ctx, target)
        self._register_cancel(job_id, sc, dc)
        src = self._count_from(sc, sfrom, source.get("filter"), stref)
        dst = self._count_from(dc, dfrom, target.get("filter"), dtref)
        _close_connectors(sc, dc)
        _log("Row counts collected from both sides")
        diff = src - dst  # signed: positive ⇒ source has more rows than target
        # Percentage is measured against the target (the expected/baseline count).
        diff_pct = round(abs(diff) / max(dst, 1) * 100, 4)
        # Direction constrains the allowed sign; tolerance bounds the magnitude. Legacy
        # `operator` (= / >= / <= / > / <) is still honoured.
        direction = options.get("direction") or _dir_from_operator(options.get("operator", "="))
        tol_pct = float(options.get("tolerance_pct", 0) or 0)
        tol_rows = int(options.get("tolerance_rows", 0) or 0)
        has_tol = tol_pct > 0 or tol_rows > 0
        # Sign check: may the source legitimately be higher/lower than the target?
        sign_ok = (
            diff >= 0 if direction == "source_higher"
            else diff <= 0 if direction == "source_lower"
            else True
        )
        # Magnitude check: within EITHER the % or the row budget. With no budget, "both"
        # means exact; a directional rule means any amount in that direction.
        if has_tol:
            mag_ok = abs(diff) <= tol_rows or diff_pct <= tol_pct
        else:
            mag_ok = diff == 0 if direction == "both" else True
        match = sign_ok and mag_ok
        summary = {
            "src_count": src,
            "dst_count": dst,
            "diff": diff,
            "diff_pct": diff_pct,
            "direction": direction,
            "tolerance_pct": tol_pct,
            "tolerance_rows": tol_rows,
            "match": bool(match),
        }
        return summary, [], [], []

    def _run_schema(self, ctx, source, target, options, job_id=None, rlog=None):  # noqa: ANN001
        _log = rlog.log if rlog is not None else (lambda *a, **k: None)
        sc, sfrom, stref = self._side(ctx, source)
        dc, dfrom, dtref = self._side(ctx, target)
        self._register_cancel(job_id, sc, dc)
        _log("Comparing schemas (columns & types)")
        # Columns the user removed from the mapping are excluded from the comparison entirely.
        ignore = {str(c).upper() for c in (options.get("ignore_columns") or [])}
        # An explicit column mapping lets differently-named columns line up (src → dst).
        cmap = {k.upper(): v.upper() for k, v in (options.get("column_mappings") or {}).items()}
        src_cols = {
            c.name.upper(): c
            for c in self._columns(sc, sfrom, stref, source.get("filter"))
            if c.name.upper() not in ignore
        }
        dst_cols = {
            c.name.upper(): c
            for c in self._columns(dc, dfrom, dtref, target.get("filter"))
            if c.name.upper() not in ignore
        }
        # Rename target columns to their mapped source name so they align by key, not literal.
        if cmap:
            dst_cols = {cmap.get(k, k): c for k, c in dst_cols.items()}
        order = list(src_cols) + [n for n in dst_cols if n not in src_cols]
        records, matched, src_only, dst_only, type_mm, null_mm = [], 0, 0, 0, 0, 0
        for name in order:
            s_c, d_c = src_cols.get(name), dst_cols.get(name)
            if s_c and d_c:
                cmatch = canonicalize(s_c.data_type) == canonicalize(d_c.data_type)
                nmatch = s_c.nullable == d_c.nullable
                status = "match" if cmatch else "type_mismatch"
                matched += 1 if cmatch else 0
                type_mm += 0 if cmatch else 1
                null_mm += 0 if nmatch else 1
                rec = {
                    "column": name,
                    "src_type": s_c.data_type,
                    "dst_type": d_c.data_type,
                    "canonical_src": canonicalize(s_c.data_type).value,
                    "canonical_dst": canonicalize(d_c.data_type).value,
                    "canonical_match": cmatch,
                    "nullability_match": nmatch,
                    "status": status,
                }
            elif s_c:
                src_only += 1
                rec = {
                    "column": name,
                    "src_type": s_c.data_type,
                    "dst_type": None,
                    "canonical_src": canonicalize(s_c.data_type).value,
                    "canonical_dst": None,
                    "canonical_match": False,
                    "nullability_match": False,
                    "status": "source_only",
                }
            else:
                dst_only += 1
                rec = {
                    "column": name,
                    "src_type": None,
                    "dst_type": d_c.data_type,
                    "canonical_src": None,
                    "canonical_dst": canonicalize(d_c.data_type).value,
                    "canonical_match": False,
                    "nullability_match": False,
                    "status": "target_only",
                }
            records.append(rec)
        summary = {
            "columns_total": len(order),
            "matched": matched,
            "source_only": src_only,
            "target_only": dst_only,
            "type_mismatches": type_mm,
            "nullability_mismatches": null_mm,
            "match": src_only == 0 and dst_only == 0 and type_mm == 0,
        }
        _close_connectors(sc, dc)
        return summary, records, [], []

    def _run_data(self, ctx, source, target, options, job_id=None, rlog=None):  # noqa: ANN001
        _log = rlog.log if rlog is not None else (lambda *a, **k: None)
        sc, sfrom, stref = self._side(ctx, source)
        dc, dfrom, dtref = self._side(ctx, target)
        self._register_cancel(job_id, sc, dc)
        _log(f"Resolved sides — source {sfrom}, target {dfrom}")
        src_schema = self._columns(sc, sfrom, stref, source.get("filter"))
        dst_schema = self._columns(dc, dfrom, dtref, target.get("filter"))
        # Columns the user chose to skip are excluded from the data compare entirely — from mapping,
        # the sample gate, and the engines. (Without this, ignore_columns only affected the schema
        # compare, so a skipped column still dirtied every hash bucket and forced the slow path.)
        ignore = {str(c).upper() for c in (options.get("ignore_columns") or [])}
        src_names = [c.name for c in src_schema if c.name.upper() not in ignore]
        dst_names = [c.name for c in dst_schema if c.name.upper() not in ignore]
        col_types = {c.name: c.data_type for c in src_schema if c.name.upper() not in ignore}
        warnings: list = []
        gate_systematic: list[str] = []  # columns the sample gate found ~100% different (flagged red)

        # source-side key. A test case can be submitted WITHOUT a key — we detect one here, at run
        # time, and record the outcome on the job so the mapping UI can show "keys detected in last
        # run on <date>" (or "not detected"). Explicit key → 'user'; detected → 'detected'; none →
        # 'not_detected' (a data compare can't line up rows, so the run fails with a clear message).
        user_key = options.get("key") or options.get("source_key")
        source_key = user_key or self._detect_key(sc, sfrom, src_schema, source.get("filter"))
        key_status = "user" if user_key else ("detected" if source_key else "not_detected")
        self._persist_key_detection(ctx, job_id, source_key, key_status)
        if not source_key:
            _log("No unique key could be detected — cannot line up rows for a data compare.")
            raise ValidationError(
                "No unique key could be detected for this data compare. Add a key in the Mapping "
                "step (or options.key) and re-run.",
                details={"key_status": "not_detected"},
            )
        _log(f"Key columns ({key_status}): {', '.join(source_key)}")

        # column mapping — align source→target even when names differ (ETL renames etc.)
        column_mappings, map_warn = self._resolve_mapping(src_names, dst_names, options)
        warnings.extend(map_warn)

        # target-side key: explicit, else mapped from the source key
        target_key = options.get("target_key")
        if not target_key:
            if column_mappings:
                from dvp.testing_engine.column_mapping import map_keys

                km = map_keys(source_key, dst_names, column_mappings)
                target_key = [km.get(k, k) for k in source_key]
            else:
                target_key = list(source_key)

        key = source_key  # results are always reported under SOURCE names
        key_up = {k.upper() for k in source_key}

        # columns to compare (source names) and their target counterparts
        if options.get("columns"):
            columns = [c for c in options["columns"] if c.upper() not in key_up]
        elif column_mappings:
            columns = [s for s in src_names if s in column_mappings and s.upper() not in key_up]
        else:
            dst_up = {n.upper() for n in dst_names}
            columns = [s for s in src_names if s.upper() in dst_up and s.upper() not in key_up]
        dst_columns = (
            [column_mappings.get(c, c) for c in columns] if column_mappings else list(columns)
        )
        # Target-side types keyed by the SOURCE column name, so the in-memory comparator can pick
        # the right rule when a column's type differs across sources (NUMBER vs VARCHAR, DATE vs
        # TIMESTAMP). `columns` and `dst_columns` are index-aligned.
        _dst_type_by_name = {c.name: c.data_type for c in dst_schema}
        dst_col_types = {
            src_col: _dst_type_by_name[dst_col]
            for src_col, dst_col in zip(columns, dst_columns, strict=False)
            if dst_col in _dst_type_by_name
        }

        src_all = list(source_key) + columns
        dst_all = list(target_key) + dst_columns

        # "Extra" (non-compared) columns — every table column that isn't a key or a compared
        # column (including ignore-listed ones). Captured only when the user opts in, for results
        # context; fetched alongside but NEVER fed to the match verdict. `src_all`/`dst_all` stay
        # unchanged so the target→source rename below (positional) keeps aligning compared columns.
        if options.get("show_extra_columns"):
            cmp_up = {c.upper() for c in columns}
            src_extra = [c for c in src_names if c.upper() not in key_up and c.upper() not in cmp_up]
            dkey_up = {k.upper() for k in target_key}
            dcmp_up = {c.upper() for c in dst_columns}
            dst_extra = [c for c in dst_names if c.upper() not in dkey_up and c.upper() not in dcmp_up]
            src_up = {c.upper() for c in src_extra}
            extras = src_extra + [c for c in dst_extra if c.upper() not in src_up]
        else:
            src_extra = dst_extra = extras = []
        src_fetch = src_all + src_extra
        dst_fetch = dst_all + dst_extra
        # Data validation fails fast by DEFAULT: on huge warehouse tables that differ a lot,
        # confirming "they differ" by reading every divergent row runs for hours and keeps
        # spawning queries. Fail-fast stops once the verdict is settled. Every run is also
        # bounded by a query budget and a wall-clock deadline so it can never run unbounded.
        import time as _time

        fail_fast = _truthy(options.get("fail_fast", True))
        timeout_s = int(options.get("timeout_seconds") or DEFAULT_COMPARE_TIMEOUT_S)
        opts = CompareOptions(
            ignore_spaces=options.get("ignore_spaces", True),
            null_equals_empty=options.get("null_equals_empty", True),
            case_insensitive=options.get("case_insensitive", False),
            tolerance=float(options.get("tolerance", 0.0)),
            compare_time=bool(options.get("compare_time", True)),
            bad_record_limit=int(options.get("bad_record_limit", 1000)),
            fail_fast=fail_fast,
            max_diff_rows=int(options.get("max_diff_rows") or options.get("fail_fast_rows") or 1000),
            max_queries=int(options.get("max_queries") or DEFAULT_MAX_QUERIES),
            deadline_epoch=(_time.time() + timeout_s) if timeout_s > 0 else None,
            should_cancel=self._should_cancel(job_id),
            memory_gb=(float(options["memory_gb"]) if options.get("memory_gb") else None),
        )
        # Server-side safety net: bound each Snowflake query to the compare budget so nothing
        # keeps running on the warehouse after we've stopped (or the thread was killed).
        if timeout_s > 0:
            for _c in (sc, dc):
                if hasattr(_c, "statement_timeout_s"):
                    _c.statement_timeout_s = timeout_s

        # Engine selection — decided BEFORE pulling rows so a pushdown never moves the table.
        #   auto (default): same-DB + numeric key → recursive hash (compare in-database);
        #                   else Snowflake↔Snowflake → hash pushdown; else in-memory.
        #   recursive: force the same-database recursive range-checksum compare.
        #   hash: per-key hash pushdown (Snowflake).  spark: scale-out.  inmemory: pure Python.
        from dvp.testing_engine import recursive_hash, snowflake_hash
        from dvp.testing_engine.typemap import CanonicalType, canonicalize

        requested = str(options.get("engine", "auto")).lower()

        # same physical database ⇒ one connector can query both tables without moving data
        same_conn_id = bool(
            source.get("connection_id")
            and source.get("connection_id") == target.get("connection_id")
        )
        same_db = same_conn_id or recursive_hash.same_server(sc, dc)
        key_numeric = canonicalize(col_types.get(source_key[0], "string")) == CanonicalType.NUMERIC
        use_recursive = (
            requested in ("auto", "recursive")
            and same_db
            and not column_mappings  # recursive assumes matching column names on both sides
            and key_numeric
            and recursive_hash.supported(sc, dc, source_key, opts)
        )
        if requested == "recursive" and not use_recursive:
            warnings.append(
                {
                    "code": "recursive_unavailable",
                    "message": "recursive hash needs the same database, a single numeric key, "
                    "matching column names, and no tolerance; used another engine",
                    "details": None,
                }
            )

        use_hash = (
            not use_recursive
            and requested in ("auto", "hash", "hash_force")
            and snowflake_hash.supported(sc, dc, opts)
        )
        if requested == "hash" and not use_hash and not use_recursive:
            warnings.append(
                {
                    "code": "hash_unavailable",
                    "message": "hash compare needs both sides Snowflake and no tolerance; "
                    "used row compare",
                    "details": None,
                }
            )

        _log(
            f"Engine: {'recursive_hash' if use_recursive else 'hash' if use_hash else 'in-memory'} "
            f"· fail_fast={opts.fail_fast} max_diff_rows={opts.max_diff_rows} "
            f"max_queries={opts.max_queries} timeout={'off' if not opts.deadline_epoch else 'on'}"
        )
        _t_engine = time.monotonic()
        if use_recursive:
            outcome, notes = recursive_hash.compare_recursive_hash(
                sc,  # single connector queries both sides in-database
                dc,
                src_from=sfrom,
                dst_from=dfrom,
                src_predicate=source.get("filter"),
                dst_predicate=target.get("filter"),
                key=source_key,
                columns=columns,
                column_types=col_types,
                options=opts,
            )
            for n in notes:
                warnings.append({"code": "recursive_note", "message": n, "details": None})
            engine_used = "recursive_hash"
        elif use_hash:
            # Cheap sampling tripwire BEFORE the full per-key hash compare: if a small random
            # sample already shows heavy mismatch, a full compare on huge tables would run for
            # hours to re-confirm "they differ a lot". Short-circuit with the sampled evidence.
            # Disable with options.precheck=off, or force a full compare with engine=hash_force.
            precheck_on = (
                requested != "hash_force"
                and str(options.get("precheck", "auto")).lower() not in ("off", "false", "0", "no")
            )
            pc = None
            if precheck_on:
                pc = snowflake_hash.sample_precheck(
                    sc, dc,
                    src_from=sfrom, dst_from=dfrom,
                    src_predicate=source.get("filter"), dst_predicate=target.get("filter"),
                    key=key, columns=columns, dst_key=target_key, dst_columns=dst_columns,
                    column_types=col_types, options=opts,
                    rounds=int(options.get("precheck_rounds", 3)),
                    sample_size=int(options.get("precheck_sample", 10)),
                    max_mismatch_pct=float(options.get("precheck_max_mismatch_pct", 20)),
                    min_common=(int(options["precheck_min_common"]) if options.get("precheck_min_common") else None),
                    max_rounds=(int(options["precheck_max_rounds"]) if options.get("precheck_max_rounds") else None),
                )
            if pc is not None and pc.short_circuit:
                outcome = snowflake_hash.outcome_from_precheck(pc)
                warnings.append({
                    "code": "hash_precheck_short_circuit",
                    "message": (
                        f"Sampling found heavy VALUE mismatch among rows present in both sides "
                        f"({pc.note}) — skipped the full hash compare, which would run for a very "
                        f"long time on large, heavily-mismatched tables. Results below are from the "
                        f"sample. Fix the data or re-run with engine=hash_force to compare in full."
                    ),
                    "details": None,
                })
                engine_used = "hash_sample"
            else:
                if pc is not None and pc.inconclusive:
                    # We couldn't verify via sampling — the sampled source rows were largely absent
                    # from the target. Say so plainly rather than implying a clean pre-check.
                    warnings.append({
                        "code": "hash_precheck_inconclusive",
                        "message": (
                            f"Couldn't sample-verify these tables ({pc.note}): the rows we sampled "
                            f"from the source weren't found in the target, so the quick check "
                            f"couldn't judge them. Ran the full compare below — if the key columns "
                            f"are wrong or the tables don't share keys, map/choose the key and re-run."
                        ),
                        "details": None,
                    })
                elif pc is not None and pc.sampled:
                    warnings.append({
                        "code": "hash_precheck_passed",
                        "message": f"Sample pre-check clean ({pc.note}) — ran the full hash compare.",
                        "details": None,
                    })
                outcome, notes = snowflake_hash.compare_hash(
                    sc,
                    dc,
                    src_from=sfrom,
                    dst_from=dfrom,
                    src_predicate=source.get("filter"),
                    dst_predicate=target.get("filter"),
                    key=key,
                    columns=columns,
                    dst_key=target_key,
                    dst_columns=dst_columns,
                    column_types=col_types,
                    options=opts,
                )
                for n in notes:
                    warnings.append({"code": "hash_note", "message": n, "details": None})
                engine_used = "hash"
        else:
            # Cross-database / general path. Compute cheap COUNT(*)s ONCE (when relevant): they drive
            # the engine choice (fingerprint vs streaming vs in-memory) and the memory guard.
            from dvp.testing_engine import fingerprint_compare, streaming_compare

            ncols = max(1, len(src_all))
            can_fp = requested in ("auto", "fingerprint") and fingerprint_compare.supported(
                sc, dc, source_key, opts
            )
            can_stream = requested in ("auto", "streaming") and streaming_compare.supported(
                source_key, key_numeric
            )
            need_counts = can_fp or can_stream or bool(opts.memory_gb and opts.memory_gb > 0)
            src_n = dst_n = 0
            est_gb = 0.0
            if need_counts:
                src_n = self._count_from(sc, sfrom, source.get("filter"), stref) or 0
                dst_n = self._count_from(dc, dfrom, target.get("filter"), dtref) or 0
                est_gb = _estimate_inmemory_gb(src_n + dst_n, ncols)
            # Switch off the simple "load both sides" path when forced, or (auto) when the data is big
            # enough that it would blow the memory budget or cross the row threshold. Small compares
            # stay in-memory (one pass, no windowing/checksum overhead).
            big = (bool(opts.memory_gb) and est_gb > opts.memory_gb) or (
                src_n + dst_n
            ) > _STREAM_ROW_THRESHOLD
            # ---- Stage 0: sample gate. Before trusting a hash compare, pull a small stratified
            # sample and compare it IN MEMORY (authoritative). It catches columns that differ ~100%
            # (a shifted date/timezone — flagged so the UI can mark them red) and routes: a clean
            # sample ⇒ hash; a broadly-divergent one ⇒ stream straight away (hashing would only bail).
            from dvp.testing_engine import sample_gate

            gate_systematic: list[str] = []
            gate_col_rate: dict[str, float] = {}
            gate_force_stream = False
            if can_fp or can_stream:
                try:
                    gate = sample_gate.precheck(
                        self._pull, sc, dc,
                        src_from=sfrom, dst_from=dfrom,
                        src_predicate=source.get("filter"), dst_predicate=target.get("filter"),
                        key=source_key, columns=columns, src_cols=src_all, dst_cols=dst_all,
                        column_mappings=column_mappings, column_types=col_types, options=opts, log=_log,
                    )
                    gate_systematic = gate.systematic_columns
                    gate_col_rate = gate.column_match_rate
                    if gate.note:
                        warnings.append({"code": "sample_gate", "message": gate.note, "details": None})
                    for c in gate_systematic:
                        warnings.append({
                            "code": "systematic_column",
                            "message": f"column '{c}' differs in ~100% of sampled rows — likely a systematic "
                                       f"format/timezone/encoding mismatch, not scattered data drift",
                            "details": {"column": c},
                        })
                    for c in gate.encoding_suspect_columns:
                        warnings.append({
                            "code": "hash_unreliable_column",
                            "message": f"column '{c}' matches on values but its cross-engine hash encoding "
                                       f"disagrees — compared with the authoritative row engine (not the hash)",
                            "details": {"column": c},
                        })
                    gate_force_stream = gate.decision == "stream"
                except Exception:  # noqa: BLE001 — the gate only STEERS; never fail the compare on it
                    log.warning("sample_gate_failed", exc_info=True)

            # ---- Systematic-column guard. If the sample pins ~all divergence on just 1–3 columns
            # (a shifted date/timezone/format) while the OTHER compared columns match, comparing those
            # columns would force the slow scattered path: every row's hash differs, hash-pruning
            # collapses, and we fall to row-by-row streaming (minutes→hours on a big table). Rather
            # than silently spend that, HALT and let the user choose — skip those columns (fast; the
            # rest prunes) or compare everything anyway (slow). Bypassed once the user opts into the
            # full compare via options.acknowledge_systematic.
            if (
                not options.get("acknowledge_systematic")
                and 1 <= len(gate_systematic) <= 3
                and all(
                    gate_col_rate.get(c, 1.0) >= sample_gate.ENCODING_VALUES_FLOOR
                    for c in columns
                    if c not in gate_systematic
                )
            ):
                cols_txt = ", ".join(repr(c) for c in gate_systematic)
                existing_ignore = [str(c) for c in (options.get("ignore_columns") or [])]
                _log(
                    f"Halted for decision — {len(gate_systematic)} column(s) ({cols_txt}) differ in "
                    f"~100% of the sample while the rest match. Comparing them forces the slow "
                    f"row-by-row path; skipping them lets the remaining columns prune fast."
                )
                _close_connectors(sc, dc)
                self._clear_progress(ctx, job_id)
                summary = {
                    "engine": "sample_gate",
                    "match": None,
                    "src_row_count": src_n,
                    "dst_row_count": dst_n,
                    "systematic_columns": list(gate_systematic),
                    "decision_required": {
                        "kind": "skip_systematic_columns",
                        "columns": list(gate_systematic),
                        "reason": (
                            f"{len(gate_systematic)} column(s) ({cols_txt}) differ in ~100% of the "
                            f"sampled rows while the other compared columns match. Comparing these "
                            f"forces a full row-by-row scan (slow); skip them to compare the rest fast."
                        ),
                        "rerun_skip": {"ignore_columns": existing_ignore
                                       + [c for c in gate_systematic if c not in existing_ignore]},
                        "rerun_full": {"acknowledge_systematic": True},
                    },
                }
                warnings.append({
                    "code": "decision_required",
                    "message": (
                        f"Paused for a choice: skip {cols_txt} and compare the rest (fast), or "
                        f"compare everything anyway (slow)."
                    ),
                    "details": {"columns": list(gate_systematic)},
                })
                return summary, None, key, warnings

            # Prefer the FINGERPRINT fast path (prunes matching ranges in-database — bandwidth-optimal
            # for mostly-equal tables); fall back to STREAMING (pulls everything, bounded memory) when
            # fingerprinting can't apply (e.g. a dialect with no cross-engine MD5).
            force_stream = gate_force_stream
            if can_fp and (requested == "fingerprint" or big) and not force_stream:
                _log(
                    f"Engine: fingerprint (in-DB checksum + bisection) · "
                    f"~{src_n:,} source / {dst_n:,} target rows"
                )
                try:
                    outcome, notes = fingerprint_compare.compare_fingerprint(
                        sc, dc,
                        src_from=sfrom, dst_from=dfrom,
                        src_predicate=source.get("filter"), dst_predicate=target.get("filter"),
                        key=source_key, columns=columns, dst_columns=dst_columns,
                        column_mappings=column_mappings, column_types=col_types,
                        options=opts, leaf_limit=(int(options["leaf_rows"]) if options.get("leaf_rows") else None),
                        bail_pct=float(options.get("fingerprint_bail_pct") or 0.6),
                        progress=self._progress_cb(ctx, job_id),
                        log=_log,
                    )
                except fingerprint_compare.BailToStreaming as bail:
                    # Too much diverges for bisection to prune — stream the whole table once instead
                    # of issuing a checksum query storm that pulls ~everything anyway.
                    _log(f"Fingerprint bailed: {bail}. Falling back to the streaming engine.")
                    warnings.append({
                        "code": "fingerprint_bailed",
                        "message": f"{bail} — too much divergence for hash pruning; used the streaming engine",
                        "details": None,
                    })
                    force_stream = True
                if not force_stream:
                    for n in notes:
                        warnings.append({"code": "fingerprint_note", "message": n, "details": None})
                    engine_used = "fingerprint"
                    _close_connectors(sc, dc)
                    summary = _data_summary(outcome)
                    summary["engine"] = engine_used
                    summary["systematic_columns"] = gate_systematic
                    _apply_diff_tolerance(summary, options)
                    _log(f"Engine {engine_used} completed in {int((time.monotonic() - _t_engine) * 1000)}ms")
                    self._clear_progress(ctx, job_id)
                    return summary, _outcome_detail(outcome), key, warnings
            if force_stream or (can_stream and (requested == "streaming" or big)):
                window = int(options.get("stream_window") or _stream_window_rows(opts.memory_gb, ncols))
                _log(
                    f"Engine: streaming key-range · window {window:,} rows · "
                    f"~{src_n:,} source / {dst_n:,} target rows (no full load into memory)"
                )
                outcome, notes = streaming_compare.compare_streaming(
                    pull=self._pull, src_conn=sc, dst_conn=dc,
                    src_from=sfrom, dst_from=dfrom,
                    src_predicate=source.get("filter"), dst_predicate=target.get("filter"),
                    key=source_key[0], dst_key=target_key[0],
                    src_cols=src_all, dst_cols=dst_all,
                    columns=columns, column_mappings=column_mappings,
                    column_types=col_types, dst_column_types=dst_col_types,
                    options=opts, window=window, total_rows=src_n,
                    log=_log, progress=self._progress_cb(ctx, job_id),
                )
                for n in notes:
                    warnings.append({"code": "stream_note", "message": n, "details": None})
                engine_used = "streaming"
                _close_connectors(sc, dc)
                summary = _data_summary(outcome)
                summary["engine"] = engine_used
                summary["systematic_columns"] = gate_systematic
                _apply_diff_tolerance(summary, options)
                _log(f"Engine {engine_used} completed in {int((time.monotonic() - _t_engine) * 1000)}ms")
                self._clear_progress(ctx, job_id)
                return summary, _outcome_detail(outcome), key, warnings
            # In-memory / Spark: load BOTH sides fully into RAM. Honour the memory budget — a clear,
            # actionable failure beats an OOM kill (or hours of swapping).
            if requested == "streaming" and not can_stream:
                warnings.append({
                    "code": "streaming_unavailable",
                    "message": "streaming needs a single numeric key; used the in-memory engine",
                    "details": None,
                })
            if requested == "fingerprint" and not can_fp:
                warnings.append({
                    "code": "fingerprint_unavailable",
                    "message": "fingerprint needs a single numeric key, exact match (no tolerance), "
                    "and both sides on a fingerprint-able engine; used the in-memory engine",
                    "details": None,
                })
            if opts.memory_gb and opts.memory_gb > 0:
                _log(
                    f"Memory: budget {opts.memory_gb:g} GB · estimated {est_gb:.2f} GB "
                    f"for {src_n + dst_n:,} rows × {ncols} cols (in-memory engine)"
                )
                if est_gb > opts.memory_gb:
                    raise _MemoryBudgetExceeded(
                        f"This compare needs about {est_gb:.1f} GB in memory "
                        f"({src_n + dst_n:,} rows × {ncols} columns) but the memory budget is "
                        f"{opts.memory_gb:g} GB. Raise the memory budget, add a row filter to "
                        f"compare fewer rows, or use a pushdown engine (same-database recursive "
                        f"or Snowflake hash), or the streaming engine (single numeric key) that "
                        f"compares without loading the data into memory."
                    )
            src_rows = self._pull(sc, sfrom, src_fetch, source.get("filter"))
            dst_raw = self._pull(dc, dfrom, dst_fetch, target.get("filter"))
            # rename target columns → source names so every engine compares like-named columns
            rename = dict(zip(dst_all, src_all, strict=False))
            dst_rows = (
                [{rename.get(k2, k2): v for k2, v in r.items()} for r in dst_raw]
                if column_mappings
                else dst_raw
            )
            if requested == "spark" and self.spark is not None and self.spark.available():
                from dvp.testing_engine.spark_compare import compare_spark

                spark = self.spark.get()
                src_df = _rows_to_df(spark, src_rows, src_all)
                dst_df = _rows_to_df(spark, dst_rows, src_all)
                outcome = compare_spark(
                    spark,
                    src_df,
                    dst_df,
                    key=key,
                    columns=columns,
                    column_types=col_types,
                    options=opts,
                )
                warnings.append(
                    {"code": "engine_spark", "message": "executed on Spark", "details": None}
                )
                engine_used = "spark"
            else:
                if requested == "spark":
                    warnings.append(
                        {
                            "code": "spark_unavailable",
                            "message": "Spark not available; used in-memory engine",
                            "details": None,
                        }
                    )
                outcome = compare_rows(
                    src_rows,
                    dst_rows,
                    key=key,
                    columns=columns,
                    column_types=col_types,
                    dst_column_types=dst_col_types,
                    options=opts,
                    extra_columns=extras,
                )
                engine_used = "inmemory"

        # Release the reused warehouse sessions now that every query has run.
        _close_connectors(sc, dc)
        summary = _data_summary(outcome)
        summary["engine"] = engine_used
        summary["systematic_columns"] = gate_systematic
        _apply_diff_tolerance(summary, options)
        _log(f"Engine {engine_used} completed in {int((time.monotonic() - _t_engine) * 1000)}ms")
        return summary, _outcome_detail(outcome), key, warnings

    def _resolve_mapping(self, src_names, dst_names, options):  # noqa: ANN001
        """Decide source→target column mapping. No-op when names already match (and the
        caller didn't force a mapping); otherwise run the layered mapper."""
        explicit = options.get("column_mappings") or {}
        auto = options.get("auto_map_columns", True)
        same = {n.upper() for n in src_names} == {n.upper() for n in dst_names}
        warn: list = []
        if not explicit and (same or not auto):
            return {}, warn
        from dvp.testing_engine.column_mapping import map_columns

        cm = map_columns(src_names, dst_names, provided=explicit or None)
        if cm.unmapped_source:
            warn.append(
                {
                    "code": "columns_unmapped",
                    "message": f"{len(cm.unmapped_source)} source column(s) had no target match",
                    "details": {"unmapped": cm.unmapped_source[:20]},
                }
            )
        return cm.mappings, warn

    def suggest_mapping(
        self, ctx: TenantContext, *, source: dict, target: dict,
        sample_size: int = 200, detect_key: bool = True, mode: str = "fuzzy",
    ) -> dict:
        """Preview column mapping (with method & confidence) so the UI can show and let the user
        confirm before running. Two modes:

          * ``fuzzy`` (default) — match on COLUMN NAMES only (exact/prefix/abbreviation/suffix).
            Instant, metadata-only. Columns the names can't resolve come back unmapped.
          * ``deep``  — additionally SAMPLE DATA and match leftover columns by value overlap
            (Jaccard), so renamed/opaque columns line up. Slower (reads rows); each such match
            carries a probability the user confirms.

        Key detection also samples data, so it's OPT-IN via ``detect_key`` (the UI maps columns
        first and only detects keys on explicit request)."""
        from dvp.testing_engine.column_mapping import map_columns, map_keys

        sc, sfrom, stref = self._side(ctx, source)
        dc, dfrom, dtref = self._side(ctx, target)
        src_cols = self._columns(sc, sfrom, stref, source.get("filter"))
        dst_cols = self._columns(dc, dfrom, dtref, target.get("filter"))
        src_names = [c.name for c in src_cols]
        dst_names = [c.name for c in dst_cols]
        src_types = {c.name: c.data_type for c in src_cols}
        dst_types = {c.name: c.data_type for c in dst_cols}
        warnings: list = []

        cm = map_columns(src_names, dst_names)
        # Deep mode: fall back to data-similarity for whatever the names couldn't resolve.
        if mode == "deep" and cm.unmapped_source and cm.unmapped_target:
            try:
                s_samp = self._samples(
                    sc, sfrom, cm.unmapped_source, source.get("filter"), sample_size
                )
                d_samp = self._samples(
                    dc, dfrom, cm.unmapped_target, target.get("filter"), sample_size
                )
                cm = map_columns(src_names, dst_names, src_samples=s_samp, dst_samples=d_samp)
            except Exception as exc:  # noqa: BLE001
                warnings.append(
                    {"code": "data_similarity_skipped", "message": str(exc), "details": None}
                )

        source_key, ratio, key_method = [], 0.0, "none"
        if detect_key:
            source_key, ratio, key_method, key_warn = self._detect_source_key(
                sc, sfrom, stref, src_names, source.get("filter")
            )
            warnings.extend(key_warn)
        km = map_keys(source_key, dst_names, cm.mappings)
        target_key = [km.get(k, k) for k in source_key]

        return {
            "mode": mode,
            "source_columns": src_names,
            "target_columns": dst_names,
            "source_column_types": src_types,
            "target_column_types": dst_types,
            "source_key": source_key,
            "target_key": target_key,
            "key_method": key_method,
            "key_uniqueness_pct": round(ratio * 100, 2),
            "mappings": [
                {
                    "source": s,
                    "target": cm.mappings[s],
                    "method": cm.method[s],
                    "confidence": cm.confidence[s],
                    # Type compatibility (canonicalised, so VARCHAR≡STRING, NUMBER≡DECIMAL don't
                    # false-flag). A real mismatch (e.g. NUMBER→VARCHAR) is the #1 false-diff cause.
                    "src_type": src_types.get(s),
                    "dst_type": dst_types.get(cm.mappings[s]),
                    "type_match": (
                        canonicalize(src_types.get(s, "")) == canonicalize(dst_types.get(cm.mappings[s], ""))
                    ),
                }
                for s in cm.mappings
            ],
            "unmapped_source": cm.unmapped_source,
            "unmapped_target": cm.unmapped_target,
            "warnings": warnings,
        }

    def detect_keys(self, ctx: TenantContext, *, source: dict, target: dict | None = None) -> dict:
        """Detect candidate key columns for one dataset on demand (declared PK if any, else a
        minimal unique key from sampled data). Separate from mapping because it samples data and
        can be slow; returns the source key and — when a target is given — the mapped target key."""
        from dvp.testing_engine.column_mapping import map_columns, map_keys

        sc, sfrom, stref = self._side(ctx, source)
        src_names = [c.name for c in self._columns(sc, sfrom, stref, source.get("filter"))]
        source_key, ratio, key_method, warnings = self._detect_source_key(
            sc, sfrom, stref, src_names, source.get("filter")
        )
        target_key = source_key
        if target is not None:
            dc, dfrom, dtref = self._side(ctx, target)
            dst_names = [c.name for c in self._columns(dc, dfrom, dtref, target.get("filter"))]
            cm = map_columns(src_names, dst_names)
            km = map_keys(source_key, dst_names, cm.mappings)
            target_key = [km.get(k, k) for k in source_key]
        return {
            "source_key": source_key,
            "target_key": target_key,
            "key_method": key_method,
            "key_uniqueness_pct": round(ratio * 100, 2),
            "warnings": warnings,
        }

    def _detect_source_key(self, sc, sfrom, stref, src_names, predicate):  # noqa: ANN001
        """Prefer a declared primary key; else detect a minimal unique key from sampled data.
        Returns (source_key, uniqueness_ratio, method, warnings)."""
        from dvp.testing_engine.column_mapping import detect_minimal_key

        pk = self._primary_key(sc, stref)
        if pk:
            return pk, 1.0, "primary_key", []
        try:
            src_sample = self._pull(sc, sfrom, src_names, predicate, limit=2000)
            source_key, ratio = detect_minimal_key(src_names, src_sample)
            return source_key, ratio, ("detected" if source_key else "none"), []
        except Exception as exc:  # noqa: BLE001
            return [], 0.0, "none", [{"code": "key_detection_skipped", "message": str(exc), "details": None}]

    def _samples(self, connector, from_sql, cols, predicate, limit):  # noqa: ANN001
        rows = self._pull(connector, from_sql, cols, predicate, limit=limit)
        out: dict[str, set] = {c: set() for c in cols}
        for r in rows:
            for c in cols:
                v = r.get(c)
                if v is not None:
                    out[c].add(str(v))
        return out

    def _writer(self, ctx: TenantContext) -> ResultStore:
        from dvp.services.sample_location import configured_writer

        return configured_writer(self.store, self.secrets, ctx) or self.results

    def _reader(self, ctx: TenantContext, ref: str | None) -> ResultStore:
        from dvp.services.sample_location import store_for_ref

        return store_for_ref(self.store, self.secrets, ctx, ref, self.results)

    @staticmethod
    def _primary_key(connector, tref) -> list[str]:  # noqa: ANN001
        """Declared primary-key columns for a table, if the source can report them (best
        effort — SQL sides and sources without PK introspection return [])."""
        if tref is None or not hasattr(connector, "primary_key"):
            return []
        try:
            return list(connector.primary_key(tref) or [])
        except Exception:  # noqa: BLE001 - PK introspection is advisory, never fatal
            return []

    # ----- connector helpers -----
    def _side(self, ctx: TenantContext, spec: dict):
        """Resolve a compare side to (connector, from_sql, tref).

        A side is a **table / view** (`table` or `view`) OR an arbitrary **SQL query**
        (`sql`). `from_sql` is the FROM target — `"schema"."table"` for a table/view, or
        `(<query>)` for SQL — so every downstream op (count/schema/data/key-detect) is
        agnostic to which it is. Any combination across source & target is allowed."""
        from dvp.connectors.registry import build_connector

        with self.store.session(ctx) as s:
            conn = s.get(Connection, spec.get("connection_id"))
            if conn is None or conn.group_id != ctx.group_id:
                raise ValidationError(
                    "connection not found", details={"id": spec.get("connection_id")}
                )
            source_type, coords = conn.source_type, dict(conn.coordinates)
            auth, secret_ref = conn.auth_method, conn.secret_ref
        secret = self.secrets.get(ctx, secret_ref) if (secret_ref and self.secrets) else None
        connector = build_connector(source_type, coords, auth, secret)

        sql = spec.get("sql")
        if sql:
            return connector, f"({sql.strip().rstrip(';')})", None
        table = spec.get("table") or spec.get("view")
        if not table:
            raise ValidationError("each compare side needs a 'table' (or 'view') or a 'sql'")
        database = spec.get("database") or coords.get("database") or ""
        schema = spec.get("schema") or coords.get("schema") or ""
        tref = TableRef(database=database, schema=schema, table=table)
        return connector, self._table_sql(connector, tref), tref

    @staticmethod
    def _table_sql(connector, tref: TableRef) -> str:  # noqa: ANN001
        ref = getattr(connector, "sql_ref", None)
        return ref(tref) if callable(ref) else tref.fqn()

    @staticmethod
    def _select_sql(connector, collist, from_sql, predicate=None, limit=None, order_by=None):  # noqa: ANN001
        where = f" WHERE {predicate}" if predicate else ""
        base = f"SELECT {collist} FROM {from_sql}{where}"
        if order_by:
            base += f" ORDER BY {order_by}"
        if limit:
            # Dialect-aware row cap: Oracle has no LIMIT (uses a ROWNUM wrapper). With ORDER BY now
            # inside `base`, the wrapper becomes the correct Oracle ordered-limit idiom (order in the
            # subquery, ROWNUM applied outside) — and plain LIMIT keeps the order elsewhere.
            sample = getattr(connector, "sample_sql", None)
            return sample(base, int(limit)) if callable(sample) else f"{base} LIMIT {int(limit)}"
        return base

    def _count_from(self, connector, from_sql, predicate=None, tref=None):  # noqa: ANN001
        # A plain table side uses the connector's native count (sources like inline/file can't
        # run arbitrary SQL); a SQL side falls back to a COUNT(*) over the subquery.
        if tref is not None and hasattr(connector, "count"):
            try:
                return int(connector.count(tref, predicate))
            except NotImplementedError:
                pass
        res = connector.execute(self._select_sql(connector, "COUNT(*)", from_sql, predicate))
        return int(res.rows[0][0])

    def _columns(self, connector, from_sql, tref, predicate=None):  # noqa: ANN001
        """Columns for a side. Table/view → real types from the catalog; SQL → names +
        types inferred from a sample (the query defines its own output columns)."""
        if tref is not None:
            return connector.get_schema(tref)
        from dvp.connectors.base import ColumnDef
        from dvp.connectors.file_source import _infer_type

        res = connector.execute(self._select_sql(connector, "*", from_sql, predicate, limit=500))
        cols = []
        for i, name in enumerate(res.columns):
            vals = [r[i] for r in res.rows if r[i] is not None]
            cols.append(
                ColumnDef(
                    name=name,
                    data_type=_infer_type(vals),
                    nullable=any(r[i] is None for r in res.rows),
                )
            )
        return cols

    def _pull(self, connector, from_sql, cols, predicate=None, limit=None, order_by=None):  # noqa: ANN001
        collist = ", ".join('"' + c.replace('"', '""') + '"' for c in cols) if cols else "*"
        res = connector.execute(
            self._select_sql(connector, collist, from_sql, predicate, limit, order_by)
        )
        return [dict(zip(res.columns, row, strict=False)) for row in res.rows]

    def _detect_key(self, connector, from_sql, schema, predicate):  # noqa: ANN001
        cols_meta = [ColumnMeta(c.name, c.data_type, c.nullable) for c in schema]

        def sample_ratio(cols: list[str]) -> float:
            sample = self._pull(connector, from_sql, cols, predicate, limit=2000)
            if not sample:
                return 0.0
            distinct = len({tuple(r.get(c) for c in cols) for r in sample})
            return distinct / len(sample)

        def validate_full(cols: list[str]) -> bool:
            total = self._count_from(connector, from_sql, predicate)
            coll = ", ".join('"' + c + '"' for c in cols)
            where = f" WHERE {predicate}" if predicate else ""
            sub = f"SELECT 1 FROM {from_sql}{where} GROUP BY {coll}"
            distinct = int(connector.execute(f"SELECT COUNT(*) FROM ({sub}) d").rows[0][0])
            return total == distinct

        return detect_key(
            cols_meta, sample_distinct_ratio=sample_ratio, validate_unique_full=validate_full
        )

    def read_differences(
        self,
        ctx: TenantContext,
        job_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
        bucket: str | None = None,
    ) -> list[dict]:
        """Paged sample rows. For data compares each row carries a `bucket`
        (match | value_difference | source_only | target_only | ambiguous); pass
        `bucket` to tab through a single category."""
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            self._load(s, ctx, job_id)
            result = s.scalar(select(CompareResult).where(CompareResult.compare_job_id == job_id))
            if result is None or not result.detail_ref:
                return []
            ref = result.detail_ref
        reader = self._reader(ctx, ref)
        if bucket is None:
            return reader.read_detail(ctx, ref, offset=offset, limit=limit)
        # Samples are capped, so read the full set and filter/paginate in-process.
        rows = [
            r
            for r in reader.read_detail(ctx, ref, offset=0, limit=100000)
            if r.get("bucket") == bucket
        ]
        return rows[offset : offset + limit]

    def export_differences_csv(self, ctx: TenantContext, job_id: str) -> str:
        """Every captured difference as flat CSV rows (bucket, key, column, source, target) — the
        whole stored set, not just the on-screen page. Value differences emit one row per differing
        cell; source/target-only emit one row per column of the present side; ambiguous emits the
        per-side counts. (The stored set is itself capped at the run's bad_record_limit per bucket.)"""
        import csv
        import io

        self.get_job(ctx, job_id)  # authorize + 404 outside the group
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["bucket", "key", "column", "source", "target"])

        def key_str(k) -> str:  # noqa: ANN001
            return ", ".join(f"{kk}={vv}" for kk, vv in (k or {}).items()) if isinstance(k, dict) else str(k)

        for bucket in ("value_difference", "source_only", "target_only", "ambiguous"):
            # limit well above any bad_record_limit so we emit everything stored for the bucket.
            for r in self.read_differences(ctx, job_id, offset=0, limit=1_000_000, bucket=bucket):
                ks = key_str(r.get("key"))
                if bucket == "value_difference":
                    for cell in r.get("cells") or []:
                        if cell.get("differs"):
                            w.writerow([bucket, ks, cell.get("column"), cell.get("src"), cell.get("dst")])
                elif bucket in ("source_only", "target_only"):
                    rec = r.get("record") or {}
                    present = "source" if bucket == "source_only" else "target"
                    for col, val in rec.items():
                        row = [bucket, ks, col, "", ""]
                        row[3 if present == "source" else 4] = val
                        w.writerow(row)
                elif bucket == "ambiguous":
                    w.writerow([bucket, ks, "", f"src_count={r.get('src_count')}", f"dst_count={r.get('dst_count')}"])
        return out.getvalue()

    def select_project_reruns(
        self, ctx: TenantContext, project_id: str, *, only_failed: bool
    ) -> list[dict]:
        """Reset the project's test cases to 'queued' and return them for the caller to enqueue, as
        {job_id, variables, batch_id}. `only_failed` limits to runs that failed or found differences
        (a healthy pass is left alone); otherwise every test case is rerun. Batch pairs are grouped
        by batch_id so the caller can share one batch_run_id per batch."""
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            jobs = s.scalars(
                select(CompareJob).where(
                    CompareJob.group_id == ctx.group_id, CompareJob.project_id == project_id
                )
            ).all()
            match_by_job = {
                cid: (summ or {}).get("match")
                for cid, summ in s.execute(
                    select(CompareResult.compare_job_id, CompareResult.summary).where(
                        CompareResult.compare_job_id.in_([j.id for j in jobs] or [""])
                    )
                ).all()
            }
            picked: list[dict] = []
            for j in jobs:
                if only_failed:
                    unhealthy = j.status in ("failed", "cancelled") or match_by_job.get(j.id) is False
                    if not unhealthy:
                        continue
                j.status = "queued"
                j.started_at = None
                j.finished_at = None
                j.error = None
                picked.append({"job_id": j.id, "variables": dict(j.variables or {}), "batch_id": j.batch_id})
            return picked

    @staticmethod
    def _load(s, ctx: TenantContext, job_id: str) -> CompareJob:  # noqa: ANN001
        job = s.get(CompareJob, job_id)
        if job is None or job.group_id != ctx.group_id:
            raise NotFoundError("compare job not found", details={"id": job_id})
        return job


DEFAULT_PROJECT_NAME = "General"


def _ensure_default_project(s, ctx) -> str:  # noqa: ANN001
    """The group's default project id, creating 'General' if missing. System-provisioned
    (not gated by MANAGE_PROJECTS) so any editor can always create a test case even before
    an admin has set up explicit projects."""
    proj = s.scalar(
        select(Project).where(
            Project.group_id == ctx.group_id,
            Project.name == DEFAULT_PROJECT_NAME,
            Project.archived_at.is_(None),
        )
    )
    if proj is None:
        proj = Project(
            org_id=ctx.org_id,
            group_id=ctx.group_id,
            name=DEFAULT_PROJECT_NAME,
            description="Default project",
            tags=[],
            created_by=ctx.user_id,
        )
        s.add(proj)
        s.flush()
    return proj.id


def _coerce_key_value(v: object, canon: str) -> object | None:
    """Coerce one user-typed key value to its source column type. An empty string (a blank field)
    becomes None; numerics parse to int/float so they match a numeric column; booleans parse from
    common spellings. Anything that won't parse falls through unchanged (type-tolerant matching
    downstream still lines it up)."""
    if v is None:
        return None
    if isinstance(v, str) and v.strip() == "":
        return None
    if canon == "NUMERIC":
        try:
            f = float(v)  # type: ignore[arg-type]
            return int(f) if f.is_integer() else f
        except (TypeError, ValueError):
            return v
    if canon == "BOOLEAN":
        s = str(v).strip().lower()
        if s in ("true", "1", "t", "yes", "y"):
            return True
        if s in ("false", "0", "f", "no", "n"):
            return False
    return v


def _coerce_key_values(
    key: list[str], key_values: list[list], key_types: dict[str, str]
) -> list[tuple]:
    """Turn the user's list of key tuples into coerced tuples ready for the key predicate. Drops any
    tuple with the wrong arity or a blank (None) part — a key lookup needs every column filled."""
    out: list[tuple] = []
    for raw in key_values or []:
        if not isinstance(raw, (list, tuple)) or len(raw) != len(key):
            continue
        # Pair each key column NAME with the value at the same position, coerce to that column's type.
        t = tuple(_coerce_key_value(v, key_types.get(k, "STRING")) for k, v in zip(key, raw, strict=False))
        if all(v is not None for v in t):
            out.append(t)
    return out


def _spec_label(spec: dict) -> str:
    """A short human label for one compare side — table name, file, or '(SQL)'."""
    if spec.get("sql"):
        return "(SQL)"
    name = spec.get("table") or spec.get("file") or spec.get("path")
    return str(name) if name else "?"


def _side_table_ref(side: dict) -> str | None:
    table = side.get("table")
    if not table:
        return None
    if "." in str(table):
        return str(table)
    parts = [p for p in (side.get("database"), side.get("schema"), table) if p]
    return ".".join(str(p) for p in parts)


def _iso(dt) -> str | None:  # noqa: ANN001
    return dt.isoformat() if dt else None


def _run_verdict(status: str, summary: dict) -> str | None:
    """Classify a recorded run: 'pass' (matched), 'diff' (ran, differed), 'fail' (errored).
    Cancelled runs return None so they neither trigger a regression nor count as a recovery."""
    if status == "cancelled":
        return None
    if status == "failed":
        return "fail"
    return "pass" if summary.get("match") is True else "diff"


def _next_run(cron: str | None) -> str | None:
    """The next fire time for a cron schedule (ISO), or None if unscheduled/invalid."""
    if not cron:
        return None
    from datetime import UTC, datetime

    nxt = next_due(cron, datetime.now(UTC))
    return nxt.isoformat() if nxt else None


def _error_hint(error: str | None) -> str | None:
    """Classify a run error into a plain-English, actionable hint so the UI can tell the user WHY
    a test 'did not run' (connection / permission / missing object / SQL) — not just that it failed."""
    if not error:
        return None
    e = error.lower()
    if any(w in e for w in (
        "timeout", "timed out", "connection refused", "could not connect", "unable to connect",
        "unreachable", "getaddrinfo", "name or service not known", "no route to host",
    )):
        return "Looks like a connection problem — check the connection is reachable and its credentials are valid (Admin → Connections → Test)."
    if any(w in e for w in (
        "permission denied", "access denied", "not authorized", "unauthorized",
        "insufficient privilege", "insufficient privileges", "privilege", "grant ",
    )):
        return "Looks like a permissions issue — the connection's user may lack SELECT on this table or schema."
    if any(w in e for w in (
        "does not exist", "doesn't exist", "not found", "no such table", "unknown table",
        "invalid object", "invalid identifier", "undefined table", "unknown column", "no such column",
    )):
        return "A referenced table or column doesn't exist — it may have been renamed or dropped (schema drift). Re-check the source/target definition."
    if any(w in e for w in ("syntax", "sql compilation", "parse error", "unexpected token", "near \"")):
        return "Looks like a SQL error — check the SQL or table definition on the source/target."
    return None


def _job_dict(j: CompareJob) -> dict:
    return {
        "id": j.id,
        "name": j.name,
        "batch_id": j.batch_id,
        "batch_name": j.batch_name,
        "project_id": j.project_id,
        "source_system": j.source_system,
        "layer": j.layer,
        "tags": j.tags or [],
        "kind": j.kind,
        "source": j.source,
        "target": j.target,
        "options": j.options,
        "status": j.status,
        # Run-time key-detection outcome (submit without a key → detected/not-detected at run).
        "key_status": j.key_status,
        "detected_key": j.detected_key or [],
        "key_detected_at": _iso(j.key_detected_at),
        "error": j.error,
        "error_hint": _error_hint(j.error),
        "has_log": bool(j.execution_log),
        # Live progress while a long run is in flight ({pct, processed, total}); None otherwise.
        "progress": j.progress,
        # Recurring schedule (cron) + the next fire time, so the UI can show "Daily · next 02:00".
        "schedule_cron": j.schedule_cron,
        "next_run": _next_run(j.schedule_cron),
        # Attribution — who created the test case + who ran it last (resolved to names by
        # `_attach_actor_names` on the list/detail paths that have a session).
        "created_by": j.created_by,
        "last_run_by": j.last_run_by,
        # Execution timing — drives the per-project "Executions" history + last-run display.
        "created_at": _iso(j.created_at),
        "started_at": _iso(j.started_at),
        "finished_at": _iso(j.finished_at),
        "updated_at": _iso(j.updated_at),
    }


def _result_dict(r: CompareResult) -> dict:
    return {
        "summary": r.summary,
        "detected_key": r.detected_key,
        "warnings": r.warnings,
        "detail_ref": r.detail_ref,
        "sample_location": _sample_location(r.detail_ref),
        "variables": r.variables or {},
    }


def _sample_location(ref: str | None) -> str | None:
    parsed = parse_warehouse_ref(ref or "")
    return parsed.location if parsed else None


def _job_summary(j: dict) -> dict:
    return j.get("summary") or (j.get("result") or {}).get("summary") or {}


def _rows_to_df(spark, rows: list[dict], cols: list[str]):  # noqa: ANN001
    """Build a Spark DataFrame from pulled rows with an explicit all-string schema so
    inference never trips on empty/mixed columns (the compare normalizes per type)."""
    from pyspark.sql.types import StringType, StructField, StructType

    schema = StructType([StructField(c, StringType(), True) for c in cols])
    data = [tuple(None if r.get(c) is None else str(r.get(c)) for c in cols) for r in rows]
    return spark.createDataFrame(data, schema)


def _batch_summary(batch_id: str, name: str, kind: str, jobs: list[dict]) -> dict:
    succeeded = sum(1 for j in jobs if j["status"] == "succeeded")
    failed = sum(1 for j in jobs if j["status"] == "failed")
    cancelled = sum(1 for j in jobs if j["status"] == "cancelled")
    matched = sum(1 for j in jobs if _job_summary(j).get("match") is True)
    differed = sum(1 for j in jobs if _job_summary(j).get("match") is False)
    running = sum(1 for j in jobs if j["status"] in ("queued", "running"))
    if running:
        status = "running"  # async: some pairs still executing on the worker tier
    elif cancelled and succeeded == 0 and failed == 0:
        status = "cancelled"
    elif failed == 0 and cancelled == 0:
        # Every pair ran without error. "succeeded" here means they ran, NOT that
        # they matched — so only call it a clean pass when nothing differed.
        status = "succeeded" if differed == 0 else "differences"
    elif succeeded == 0 and cancelled == 0:
        status = "failed"
    else:
        status = "partial"  # mix of ok / failed / cancelled
    finished = [j.get("finished_at") for j in jobs if j.get("finished_at")]
    created = [j.get("created_at") for j in jobs if j.get("created_at")]
    first = jobs[0] if jobs else {}
    return {
        "batch_id": batch_id,
        "name": name,
        "kind": kind,
        "status": status,
        "project_id": first.get("project_id"),
        "total": len(jobs),
        "succeeded": succeeded,
        "failed": failed,
        "matched": matched,
        "differed": differed,
        "running": running,
        "cancelled": cancelled,
        "created_by": first.get("created_by"),
        "created_by_name": first.get("created_by_name"),
        "last_run_by": first.get("last_run_by"),
        "last_run_by_name": first.get("last_run_by_name"),
        "created_at": min(created) if created else None,
        "finished_at": max(finished) if finished else None,
        # Schedule is set on every pair as a unit, so the batch's schedule is any pair's (use first).
        "schedule_cron": first.get("schedule_cron"),
        "next_run": first.get("next_run"),
        "jobs": jobs,
    }


def param_hash(variables: dict | None) -> str:
    """Stable fingerprint of a run's runtime {{variables}} so the failure trend compares only
    like-for-like parameter sets (an empty set hashes to a constant, grouping un-parameterized runs
    together)."""
    canon = json.dumps(variables or {}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def failed_count_from_summary(kind: str, summary: dict | None) -> int:
    """Normalize a compare's per-kind summary into one 'failed records' count (the number the
    trend tracks). Mirrors how the results UI reports RECORD COUNT per rule/bucket."""
    s = summary or {}
    if kind == "data":
        return int((s.get("value_difference_rows") or 0) + (s.get("source_only_rows") or 0)
                   + (s.get("target_only_rows") or 0) + (s.get("ambiguous_rows") or 0))
    if kind == "schema":
        return int((s.get("source_only") or 0) + (s.get("target_only") or 0)
                   + (s.get("type_mismatches") or 0) + (s.get("nullability_mismatches") or 0))
    if kind == "count":
        return int(abs(s.get("diff") or 0))
    if s.get("match") is False:
        return int(s.get("failed_count") or s.get("mismatched_rows") or 0)
    return 0


def failure_trend(points: list[dict]) -> dict:
    """Pure: from an oldest→newest series of ``{ts, failed_count, status}`` compute the latest
    failure count, the previous one, and the change. `direction` is 'up' = more failures (bad),
    'down' = fewer (good), 'flat', or 'none'. `pct_change` is None when the previous run had 0
    failures (a % increase from zero is undefined — the UI shows the absolute delta there)."""
    pts = [p for p in points if p.get("failed_count") is not None]
    out = {"current": None, "previous": None, "abs_delta": None, "pct_change": None,
           "direction": "none", "points": points}
    if not pts:
        return out
    current = pts[-1]["failed_count"]
    out["current"] = current
    if len(pts) < 2:
        return out
    previous = pts[-2]["failed_count"]
    delta = current - previous
    out["previous"] = previous
    out["abs_delta"] = delta
    out["direction"] = "up" if delta > 0 else "down" if delta < 0 else "flat"
    if previous:
        out["pct_change"] = round(delta / previous * 100, 2)
    elif current == 0:
        out["pct_change"] = 0.0
    return out


def _data_summary(outcome) -> dict:  # noqa: ANN001
    """Four-bucket summary for a data compare (lightweight aggregates → metadata DB)."""
    s = outcome.summary
    return {
        "src_row_count": s.src_row_count,
        "dst_row_count": s.dst_row_count,
        "keys_total": s.keys_total,
        "matched_rows": s.matched_rows,
        "match_rate_pct": s.match_rate_pct,
        "per_column_mismatch": s.per_column_mismatch,
        # non-compared columns captured for context (UI shows them flagged "not compared")
        "extra_columns": getattr(s, "extra_columns", []) or [],
        # the four buckets (bucket 4 = ambiguous, set aside for now)
        "value_difference_rows": s.mismatched_rows,  # bucket 1
        "source_only_rows": s.src_only_rows,  # bucket 2
        "target_only_rows": s.dst_only_rows,  # bucket 3
        "ambiguous_rows": s.ambiguous_rows,  # bucket 4
        # kept for back-compat with existing callers/UI
        "mismatched_rows": s.mismatched_rows,
        "src_only_rows": s.src_only_rows,
        "dst_only_rows": s.dst_only_rows,
        # fail-fast tripped: counts above are a lower bound (see _apply_diff_tolerance for fail_fast flag)
        "stopped_early": getattr(s, "stopped_early", False),
        "match": s.mismatched_rows == 0
        and s.src_only_rows == 0
        and s.dst_only_rows == 0
        and s.ambiguous_rows == 0,
    }


def _dir_from_operator(op: str) -> str:
    """Map the legacy counts `operator` to the new direction model."""
    return {"=": "both", ">=": "source_higher", ">": "source_higher",
            "<=": "source_lower", "<": "source_lower"}.get(op, "both")


def _apply_diff_tolerance(summary: dict, options: dict) -> None:
    """Data validation "allowed differences": keep the strict identity under `identical`, and
    (when the user set a budget) relax `match` to pass if the total differing rows are within
    an absolute count AND/OR a percentage of keys. No budget → `match` stays strict."""
    identical = bool(summary.get("match", False))
    summary["identical"] = identical
    total_diff = (
        int(summary.get("value_difference_rows", 0) or 0)
        + int(summary.get("source_only_rows", 0) or 0)
        + int(summary.get("target_only_rows", 0) or 0)
        + int(summary.get("ambiguous_rows", 0) or 0)
    )
    keys = int(summary.get("keys_total", 0) or 0)
    diff_pct = round(total_diff / keys * 100, 4) if keys else (0.0 if total_diff == 0 else 100.0)
    summary["total_diff_rows"] = total_diff
    summary["diff_pct"] = diff_pct
    # Surface fail-fast config so the results UI can explain an early stop (fail_fast defaults on).
    summary["fail_fast"] = _truthy(options.get("fail_fast", True))
    summary["max_diff_rows"] = int(options.get("max_diff_rows") or options.get("fail_fast_rows") or 1000)
    max_rows = options.get("max_diff_rows")
    max_pct = options.get("max_diff_pct")
    if max_rows in (None, "") and max_pct in (None, ""):
        return  # strict: match already reflects exact identity
    ok = True
    if max_rows not in (None, ""):
        ok = ok and total_diff <= int(max_rows)
    if max_pct not in (None, ""):
        ok = ok and diff_pct <= float(max_pct)
    summary["match"] = ok
    summary["tolerance"] = {"max_diff_rows": max_rows or None, "max_diff_pct": max_pct or None}


def reap_orphaned_runs(store: MetadataStore, *, include_queued: bool = False) -> int:
    """Mark jobs stranded by a process restart as failed — a system sweep run once at boot.

    A job in status ``running`` definitively had a worker that no longer exists after a restart
    (``active_runs`` is in-memory), so it would otherwise stay "Running" forever — a phantom in
    Executions and in project health. We mark those failed and record the run so they resolve.

    ``include_queued`` also reaps ``queued`` jobs — correct ONLY for the in-process (thread) queue,
    which loses pending work on restart. With a durable queue (subprocess/JobQueueStore) a queued
    job is legitimately waiting for a worker to pick it up, so leave it be.

    Runs group-wide (ctx=None) so it works for every tenant, and swallows nothing silently — it
    returns the count reaped for the caller to log. Best-effort per job: one bad row can't block boot.
    """
    statuses = ["running", "queued"] if include_queued else ["running"]
    reaped = 0
    with store.session(None) as s:
        orphans = s.scalars(select(CompareJob).where(CompareJob.status.in_(statuses))).all()
        for job in orphans:
            job.status = "failed"
            job.finished_at = utcnow()
            job.error = "interrupted — the server restarted while this run was in flight"
            log_lines = list(job.execution_log or [])
            log_lines.append(
                {
                    "ts": utcnow().isoformat(),
                    "elapsed_ms": 0,
                    "level": "error",
                    "msg": "Run abandoned: server restarted mid-execution",
                }
            )
            job.execution_log = log_lines
            CompareService._record_run(s, job, {}, None)
            reaped += 1
    return reaped


def _outcome_detail(outcome) -> list[dict]:  # noqa: ANN001
    """Flatten pass + bad samples into bucketed detail rows for the ResultStore.

    Each row carries `bucket`; real values live here (ResultStore), never the metadata
    DB (residency rule D9)."""
    rows: list[dict] = []
    for d in outcome.passed_sample:
        rows.append({"key": d.key, "bucket": d.bucket, "record": d.record})
    for d in outcome.differences:
        row: dict = {"key": d.key, "bucket": d.bucket, "status": d.status}
        if d.bucket == "value_difference":
            row["cells"] = [vars(c) for c in d.cells]
        elif d.bucket in ("source_only", "target_only"):
            row["record"] = d.record
        elif d.bucket == "ambiguous":
            row["src_count"] = d.src_count
            row["dst_count"] = d.dst_count
        rows.append(row)
    return rows
