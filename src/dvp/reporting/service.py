"""ReportService — create, generate, read, export persisted reports.

Gathers the relevant executions per the report's `spec`, aggregates them via the pure
builders, persists headline stats to the metadata DB and the full document to the
ResultStore (D9 residency), and renders exports (JSON / CSV / HTML).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from dvp.abstractions.result_store import ResultStore
from dvp.connectors.base import TableRef
from dvp.connectors.dialect import count_distinct_sql, supports_approx_distinct
from dvp.core.errors import NotFoundError, ValidationError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.base import utcnow
from dvp.db.models import (
    CompareJob,
    CompareResult,
    Connection,
    DQRule,
    Incident,
    Monitor,
    Report,
    ReportVersion,
    RuleResult,
    User,
)
from dvp.db.session import MetadataStore
from dvp.reporting import builders
from dvp.services import audit

REPORT_TYPES = ("data_validation", "data_quality", "executive", "profile")

# Above this distinct-count, profiling keeps the HyperLogLog approximation instead of an
# exact COUNT(DISTINCT) (far cheaper on large/high-cardinality columns). Engines with a
# native approximate-distinct function use it in the single profiling scan.
DEFAULT_APPROX_THRESHOLD = 1000


class ReportService:
    def __init__(
        self, store: MetadataStore, results: ResultStore, secrets=None, notifier=None
    ) -> None:  # noqa: ANN001
        self.store = store
        self.results = results
        self.secrets = secrets  # needed to profile live sources (profile report)
        self.notifier = notifier  # delivers scheduled digests (email/Slack)

    # ----------------------------- lifecycle -----------------------------
    def create_report(
        self,
        ctx: TenantContext,
        *,
        name: str,
        report_type: str,
        spec: dict | None = None,
        project_id: str | None = None,
        schedule_cron: str | None = None,
        delivery: dict | None = None,
    ) -> dict:
        ctx.require(Permission.WRITE)
        if report_type not in REPORT_TYPES:
            raise ValidationError(
                "unknown report_type", details={"report_type": report_type, "allowed": REPORT_TYPES}
            )
        with self.store.session(ctx) as s:
            rep = Report(
                org_id=ctx.org_id,
                group_id=ctx.group_id,
                name=name,
                report_type=report_type,
                project_id=project_id,
                spec=spec or {},
                schedule_cron=schedule_cron,
                delivery=delivery or {},
                status="draft",
                created_by=ctx.user_id,
            )
            s.add(rep)
            s.flush()
            audit.record(s, ctx, "report.create", entity_type="report", entity_id=rep.id)
            return _report_dict(rep)

    def generate(self, ctx: TenantContext, report_id: str) -> dict:
        """Build a NEW immutable version from current data; update the standing report's
        latest-version cache. Re-generating never overwrites prior evidence."""
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            rep = self._load(s, ctx, report_id)
            # project scope: report.project_id wins, else spec.project_id
            spec = dict(rep.spec or {})
            if rep.project_id:
                spec["project_id"] = rep.project_id
            version = rep.latest_version + 1
            try:
                summary, document = self._aggregate(ctx, rep.report_type, spec)
            except Exception as exc:  # surface failure, never silently swallow (golden rule #3)
                rep.status = "failed"
                rep.error = str(exc)
                rep.generated_at = utcnow()
                s.add(
                    ReportVersion(
                        report_id=rep.id,
                        version=version,
                        status="failed",
                        error=str(exc),
                        generated_at=utcnow(),
                        created_by=ctx.user_id,
                    )
                )
                raise
            now = utcnow()
            document["name"] = rep.name
            document["version"] = version
            document["generated_at"] = now.isoformat()
            ref = self.results.write_detail(ctx, f"report_{rep.id}_v{version}", [document])
            s.add(
                ReportVersion(
                    report_id=rep.id,
                    version=version,
                    status="ready",
                    summary=summary,
                    document_ref=ref,
                    generated_at=now,
                    created_by=ctx.user_id,
                )
            )
            # update the standing report's cache (points at the latest version)
            rep.latest_version = version
            rep.summary = summary
            rep.document_ref = ref
            rep.status = "ready"
            rep.error = None
            rep.generated_at = now
            audit.record(
                s,
                ctx,
                "report.generate",
                entity_type="report",
                entity_id=rep.id,
                detail={"version": version},
            )
            return _report_dict(rep)

    def sign_off_batch(self, ctx: TenantContext, batch_id: str) -> dict:
        """Turn a finished compare batch into the report a cutover meeting reopens.

        A second sign-off adds a version of the same report. It does not start a new one.
        """
        from dvp.testing_engine.service import CompareService

        ctx.require(Permission.WRITE)
        batch = CompareService(self.store, self.results).get_batch(ctx, batch_id)
        if any(j.get("status") in ("queued", "running") for j in batch.get("jobs") or []):
            raise ValidationError("batch is still running", details={"batch_id": batch_id})
        with self.store.session(ctx) as s:
            found = next(
                (
                    r
                    for r in s.scalars(select(Report).where(Report.group_id == ctx.group_id)).all()
                    if (r.spec or {}).get("sign_off") and (r.spec or {}).get("batch_id") == batch_id
                ),
                None,
            )
            report_id = found.id if found else None
            project_id = batch.get("project_id")
            name = f"{batch.get('name') or 'batch'} sign-off"
        if report_id is None:
            created = self.create_report(
                ctx,
                name=name,
                report_type="data_validation",
                spec={"batch_id": batch_id, "sign_off": True},
                project_id=project_id,
            )
            report_id = created["id"]
        return self.generate(ctx, report_id)

    def list_versions(self, ctx: TenantContext, report_id: str) -> list[dict]:
        ctx.require(Permission.VIEW_REPORTS)
        with self.store.session(ctx) as s:
            self._load(s, ctx, report_id)  # tenant check
            vers = s.scalars(
                select(ReportVersion)
                .where(ReportVersion.report_id == report_id)
                .order_by(ReportVersion.version.desc())
            ).all()
            return [_version_dict(v) for v in vers]

    def get_version(self, ctx: TenantContext, report_id: str, version: int) -> dict:
        ctx.require(Permission.VIEW_REPORTS)
        with self.store.session(ctx) as s:
            self._load(s, ctx, report_id)
            v = s.scalar(
                select(ReportVersion)
                .where(ReportVersion.report_id == report_id)
                .where(ReportVersion.version == version)
            )
            if v is None:
                raise NotFoundError("report version not found", details={"version": version})
            d = _version_dict(v)
            d["document"] = self._read_doc(ctx, v.document_ref) if v.document_ref else None
            return d

    def trend(self, ctx: TenantContext, report_id: str) -> dict:
        """Compare the latest version to the previous one (regression detection)."""
        ctx.require(Permission.VIEW_REPORTS)
        with self.store.session(ctx) as s:
            self._load(s, ctx, report_id)
            vers = s.scalars(
                select(ReportVersion)
                .where(ReportVersion.report_id == report_id)
                .where(ReportVersion.status == "ready")
                .order_by(ReportVersion.version.desc())
                .limit(2)
            ).all()
        if not vers:
            return {"points": [], "delta": None}
        points = [
            {
                "version": v.version,
                "pass_rate_pct": (v.summary or {}).get("pass_rate_pct"),
                "generated_at": v.generated_at.isoformat() if v.generated_at else None,
            }
            for v in reversed(vers)
        ]
        delta = None
        if len(vers) == 2:
            cur = (vers[0].summary or {}).get("pass_rate_pct")
            prev = (vers[1].summary or {}).get("pass_rate_pct")
            if cur is not None and prev is not None:
                delta = round(cur - prev, 2)
        return {"points": points, "delta": delta}

    # ----------------------------- scheduling + delivery -----------------------------
    def deliver(self, ctx: TenantContext, report_id: str, *, regenerate: bool = False) -> dict:
        """Deliver the report's latest version as a digest to its configured channels
        (report.delivery = {"email": [...], "slack": [...]}). Generates first if needed."""
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            rep = self._load(s, ctx, report_id)
            needs_gen = regenerate or rep.latest_version == 0
            delivery = dict(rep.delivery or {})
            summary = dict(rep.summary or {})
            name, rtype = rep.name, rep.report_type
        if needs_gen:
            g = self.generate(ctx, report_id)
            summary = g["summary"]
        subject = f"[{rtype}] {name}"
        headline = summary.get("pass_rate_pct", summary.get("health_score_pct"))
        body = _digest_body(name, rtype, summary)
        sent: list[dict] = []
        if self.notifier is not None:
            for channel, recipients in delivery.items():
                for recipient in recipients or []:
                    self.notifier.send(channel, recipient, subject, body)
                    sent.append({"channel": channel, "recipient": recipient})
        return {"report_id": report_id, "headline": headline, "delivered": sent}

    def due_reports(self, ctx: TenantContext, now) -> list[str]:  # noqa: ANN001
        """Report ids whose cron schedule is due at `now` (minute precision)."""
        from dvp.core.cron import is_due

        out: list[str] = []
        with self.store.session(ctx) as s:
            reps = s.scalars(
                select(Report)
                .where(Report.group_id == ctx.group_id)
                .where(Report.schedule_cron.is_not(None))
            ).all()
            for rep in reps:
                try:
                    if rep.schedule_cron and is_due(rep.schedule_cron, now):
                        out.append(rep.id)
                except ValueError:
                    continue  # malformed cron → skip, don't crash the tick
        return out

    def tick(self, ctx: TenantContext, now, *, submit=None) -> list[str]:  # noqa: ANN001
        """Generate + deliver every due report. `submit` (worker queue) runs each off the
        scheduler thread; without it, runs inline. Returns the report ids processed."""
        due = self.due_reports(ctx, now)
        for rid in due:
            if submit is not None:
                submit(self.deliver, ctx, rid, regenerate=True)
            else:
                self.deliver(ctx, rid, regenerate=True)
        return due

    def list_reports(self, ctx: TenantContext) -> list[dict]:
        ctx.require(Permission.VIEW_REPORTS)
        with self.store.session(ctx) as s:
            reps = s.scalars(
                select(Report)
                .where(Report.group_id == ctx.group_id)
                .order_by(Report.created_at.desc())
            ).all()
            return [_report_dict(r) for r in reps]

    def get_report(
        self, ctx: TenantContext, report_id: str, *, include_document: bool = True
    ) -> dict:
        ctx.require(Permission.VIEW_REPORTS)
        with self.store.session(ctx) as s:
            rep = self._load(s, ctx, report_id)
            d = _report_dict(rep)
            d["document"] = self._document(ctx, rep) if include_document else None
            return d

    def delete_report(self, ctx: TenantContext, report_id: str) -> None:
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            rep = self._load(s, ctx, report_id)
            s.delete(rep)
            audit.record(s, ctx, "report.delete", entity_type="report", entity_id=report_id)

    def export(self, ctx: TenantContext, report_id: str, fmt: str = "json") -> tuple:
        """Return (content, media_type) for a generated report. Content is str for
        json/csv/html and bytes for pdf."""
        ctx.require(Permission.VIEW_REPORTS)
        with self.store.session(ctx) as s:
            rep = self._load(s, ctx, report_id)
            doc = self._document(ctx, rep)
        if doc is None:
            raise ValidationError("report has not been generated yet", details={"id": report_id})
        return self._render(rep.name, rep.report_type, doc, fmt)

    def export_adhoc(
        self, ctx: TenantContext, *, report_type: str, spec: dict, name: str, fmt: str = "pdf"
    ) -> tuple:
        """Export a document aggregated on-the-fly from a spec (e.g. a compare batch_id or
        job_ids) WITHOUT persisting a Report — used for one-click 'export these results'.
        Aggregation is group-scoped, so a tenant can only ever export its own data."""
        ctx.require(Permission.READ)
        summary, doc = self._aggregate(ctx, report_type, dict(spec))
        doc["name"] = name
        return self._render(name, report_type, doc, fmt)

    @staticmethod
    def _render(name: str, report_type: str, doc: dict, fmt: str) -> tuple:
        fmt = (fmt or "json").lower()
        if fmt == "json":
            return json.dumps(doc, indent=2, default=str), "application/json"
        if fmt == "csv":
            return _to_csv(report_type, doc), "text/csv"
        if fmt == "html":
            return _to_html(name, report_type, doc), "text/html"
        if fmt == "pdf":
            from dvp.reporting import pdf

            if not pdf.available():
                raise ValidationError(
                    "PDF export is not installed",
                    details={"install": "pip install 'dv-platform[pdf]'"},
                )
            return pdf.render_pdf(name, report_type, doc), "application/pdf"
        raise ValidationError(
            "unsupported export format",
            details={"format": fmt, "allowed": ["json", "csv", "html", "pdf"]},
        )

    # ----------------------------- aggregation -----------------------------
    def _aggregate(self, ctx: TenantContext, report_type: str, spec: dict) -> tuple[dict, dict]:
        if report_type == "data_validation":
            return builders.build_data_validation(self._validation_jobs(ctx, spec))
        if report_type == "data_quality":
            return builders.build_data_quality(self._quality_rules(ctx, spec))
        if report_type == "profile":
            return builders.build_profile(self._profile_tables(ctx, spec))
        # executive = both + incidents
        _, v_doc = builders.build_data_validation(self._validation_jobs(ctx, {}))
        _, q_doc = builders.build_data_quality(self._quality_rules(ctx, {}))
        return builders.build_executive(v_doc, q_doc, self._incident_counts(ctx))

    def _validation_jobs(self, ctx: TenantContext, spec: dict) -> list[dict]:
        with self.store.session(ctx) as s:
            q = select(CompareJob).where(CompareJob.group_id == ctx.group_id)
            if spec.get("batch_id"):
                q = q.where(CompareJob.batch_id == spec["batch_id"])
            if spec.get("job_ids"):
                q = q.where(CompareJob.id.in_(spec["job_ids"]))
            if spec.get("kinds"):
                q = q.where(CompareJob.kind.in_(spec["kinds"]))
            if spec.get("project_id"):
                q = q.where(CompareJob.project_id == spec["project_id"])
            if spec.get("since_days"):
                q = q.where(
                    CompareJob.created_at >= utcnow() - timedelta(days=int(spec["since_days"]))
                )
            jobs = s.scalars(q.order_by(CompareJob.created_at.desc())).all()
            out = []
            for j in jobs:
                res = s.scalar(select(CompareResult).where(CompareResult.compare_job_id == j.id))
                out.append(
                    {
                        "id": j.id,
                        "name": j.name,
                        "kind": j.kind,
                        "source": j.source,
                        "target": j.target,
                        "status": j.status,
                        "source_system": j.source_system,
                        "layer": j.layer,
                        "tags": j.tags or [],
                        "summary": res.summary if res else None,
                        "warnings": res.warnings if res else [],
                        "created_by": j.created_by,
                        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
                        "detail_ref": res.detail_ref if res else None,
                    }
                )
            if spec.get("sign_off") and out:
                self._stamp_sign_off(ctx, s, out, spec.get("batch_id"))
            return out

    def _stamp_sign_off(self, ctx, s, jobs: list[dict], batch_id: str | None) -> None:  # noqa: ANN001
        """Who ran the batch, when, and the exception keys already stored on each failure."""
        times = [j["finished_at"] for j in jobs if j.get("finished_at")]
        ran_at = max(times) if times else None
        runner_id = next(
            (j["created_by"] for j in jobs if j.get("finished_at") == ran_at and j.get("created_by")),
            next((j.get("created_by") for j in jobs if j.get("created_by")), None),
        )
        user = s.get(User, runner_id) if runner_id else None
        ran_by = (user.display_name or user.email) if user else None
        for j in jobs:
            ok = builders.job_passed(j["kind"], j.get("summary")) and j.get("status") == "succeeded"
            j["sign_off"] = True
            j["ran_by"] = ran_by
            j["ran_at"] = ran_at
            j["batch_id"] = batch_id
            j["exception_keys"] = [] if ok else self._exception_keys(ctx, j.get("detail_ref"))

    def _exception_keys(self, ctx: TenantContext, detail_ref: str | None, limit: int = 20) -> list[str]:
        if not detail_ref:
            return []
        keys: list[str] = []
        from dvp.services.sample_location import store_for_ref

        reader = store_for_ref(self.store, self.secrets, ctx, detail_ref, self.results)
        for row in reader.read_detail(ctx, detail_ref, offset=0, limit=500):
            if row.get("bucket") in (None, "match", "matched") or row.get("status") == "match":
                continue
            key = row.get("key")
            if isinstance(key, dict):
                text = ", ".join(f"{name}={key[name]}" for name in key)
            elif key is None:
                continue
            else:
                text = str(key)
            if text not in keys:
                keys.append(text)
            if len(keys) >= limit:
                break
        return keys

    def _quality_rules(self, ctx: TenantContext, spec: dict) -> list[dict]:
        with self.store.session(ctx) as s:
            q = (
                select(DQRule)
                .where(DQRule.group_id == ctx.group_id)
                .where(DQRule.is_deleted.is_(False))
            )
            monitors = {
                m.id: m
                for m in s.scalars(select(Monitor).where(Monitor.group_id == ctx.group_id)).all()
            }
            # project scope selects the monitors (DQ test cases) in the project
            if spec.get("project_id"):
                scoped = {mid for mid, m in monitors.items() if m.project_id == spec["project_id"]}
                q = q.where(DQRule.monitor_id.in_(scoped or {"__none__"}))
            if spec.get("monitor_ids"):
                q = q.where(DQRule.monitor_id.in_(spec["monitor_ids"]))
            rules = s.scalars(q).all()
            out = []
            for r in rules:
                latest = s.scalar(
                    select(RuleResult)
                    .where(RuleResult.dq_rule_id == r.id)
                    .order_by(RuleResult.created_at.desc())
                    .limit(1)
                )
                mon = monitors.get(r.monitor_id)
                out.append(
                    {
                        "id": r.id,
                        "name": r.name,
                        "table_ref": r.table_ref,
                        "dimension": (r.config or {}).get("dimension") or "quality",
                        "severity": r.severity,
                        "monitor": mon.name if mon else None,
                        "source_system": mon.source_system if mon else None,
                        "layer": mon.layer if mon else None,
                        "passed": latest.passed if latest else None,
                        "metric_value": latest.metric_value if latest else None,
                        "threshold": latest.threshold if latest else None,
                        "observed": latest.observed if latest else {},
                    }
                )
            return out

    def _profile_tables(self, ctx: TenantContext, spec: dict) -> list[dict]:
        """Profile each table via pushdown SQL — row count + per-column null/empty, distinct
        (approximate above a cardinality threshold), min, max. One scan per table (plus a
        cheap exact-distinct refine over only the low-cardinality columns).
        Spec: {connection_id, schema, database, tables, approx_threshold}."""
        conn_id = spec.get("connection_id")
        tables = spec.get("tables") or []
        if not conn_id or not tables:
            raise ValidationError(
                "profile report needs spec.connection_id and spec.tables",
                details={"connection_id": conn_id, "tables": tables},
            )
        connector = self._connector(ctx, conn_id)
        database = spec.get("database") or ""
        schema = spec.get("schema") or ""
        threshold = int(spec.get("approx_threshold", DEFAULT_APPROX_THRESHOLD))
        out: list[dict] = []
        for table in tables:
            tref = TableRef(database=database, schema=schema, table=table)
            fqn = connector.sql_ref(tref) if hasattr(connector, "sql_ref") else tref.fqn()
            columns = connector.get_schema(tref)
            out.append(self._profile_one(connector, fqn, columns, table, threshold))
        return out

    def _profile_one(self, connector, fqn, columns, table, threshold) -> dict:  # noqa: ANN001
        dialect = getattr(connector, "dialect", "sqlite")
        approx_ok = supports_approx_distinct(dialect)
        text_t = "TEXT" if dialect == "sqlite" else "VARCHAR"

        def q(c: str) -> str:
            return '"' + c.replace('"', '""') + '"'

        # ---- pass 1: counts + null + empty + distinct (approx when the engine supports it) ----
        aggs = ["COUNT(*) AS __n"]
        for i, col in enumerate(columns):
            qc = q(col.name)
            distinct = count_distinct_sql(dialect, qc, approx=approx_ok)
            aggs += [
                f"COUNT({qc}) AS c{i}_nn",
                f"SUM(CASE WHEN CAST({qc} AS {text_t}) = '' THEN 1 ELSE 0 END) AS c{i}_e",
                f"{distinct} AS c{i}_d",
                f"MIN({qc}) AS c{i}_min",
                f"MAX({qc}) AS c{i}_max",
            ]
        res = connector.execute(f"SELECT {', '.join(aggs)} FROM {fqn}")
        row = dict(zip(res.columns, res.rows[0], strict=False)) if res.rows else {}
        n = int(row.get("__n", 0) or 0)

        # ---- pass 2 (only if approx): exact distinct for the low-cardinality columns, cheaply ----
        exact: dict[int, int] = {}
        if approx_ok:
            low = [i for i, _ in enumerate(columns) if int(row.get(f"c{i}_d", 0) or 0) <= threshold]
            if low:
                refine = ", ".join(f"COUNT(DISTINCT {q(columns[i].name)}) AS c{i}_d" for i in low)
                r2 = connector.execute(f"SELECT {refine} FROM {fqn}")
                row2 = dict(zip(r2.columns, r2.rows[0], strict=False)) if r2.rows else {}
                exact = {i: int(row2.get(f"c{i}_d", 0) or 0) for i in low}

        cols = []
        for i, col in enumerate(columns):
            nn = int(row.get(f"c{i}_nn", 0) or 0)
            empty = int(row.get(f"c{i}_e", 0) or 0)
            is_approx = approx_ok and i not in exact
            distinct = exact.get(i, int(row.get(f"c{i}_d", 0) or 0))
            cols.append(
                {
                    "name": col.name,
                    "data_type": col.data_type,
                    "non_null": nn,
                    "null_count": n - nn,
                    "null_pct": round((n - nn) / n * 100, 2) if n else 0.0,
                    "empty_count": empty,
                    "empty_pct": round(empty / n * 100, 2) if n else 0.0,
                    "distinct": distinct,
                    "distinct_pct": round(distinct / n * 100, 2) if n else 0.0,
                    "distinct_approx": is_approx,
                    "min": _jsonable(row.get(f"c{i}_min")),
                    "max": _jsonable(row.get(f"c{i}_max")),
                }
            )
        return {"table": table, "row_count": n, "columns": cols}

    def _connector(self, ctx: TenantContext, connection_id: str):  # noqa: ANN001
        from dvp.connectors.registry import build_connector

        with self.store.session(ctx) as s:
            conn = s.get(Connection, connection_id)
            if conn is None or conn.group_id != ctx.group_id:
                raise ValidationError("connection not found", details={"id": connection_id})
            source_type, coords = conn.source_type, dict(conn.coordinates)
            auth, secret_ref = conn.auth_method, conn.secret_ref
        secret = self.secrets.get(ctx, secret_ref) if (secret_ref and self.secrets) else None
        return build_connector(source_type, coords, auth, secret)

    def _incident_counts(self, ctx: TenantContext) -> dict:
        """Observability incidents only. Compare (reconciliation) failures are already
        summarised by the executive report's validation_pass_rate_pct, so counting them
        here too would double-surface the same problem."""
        with self.store.session(ctx) as s:
            incs = [
                i
                for i in s.scalars(select(Incident).where(Incident.group_id == ctx.group_id)).all()
                if i.kind != "compare"
            ]
            open_ = sum(1 for i in incs if i.status != "resolved")
            return {"total": len(incs), "open": open_, "resolved": len(incs) - open_}

    # ----------------------------- helpers -----------------------------
    def _load(self, s, ctx: TenantContext, report_id: str) -> Report:  # noqa: ANN001
        rep = s.get(Report, report_id)
        if rep is None or rep.group_id != ctx.group_id:
            raise NotFoundError("report not found", details={"id": report_id})
        return rep

    def _document(self, ctx: TenantContext, rep: Report) -> dict | None:
        return self._read_doc(ctx, rep.document_ref)

    def _read_doc(self, ctx: TenantContext, ref: str | None) -> dict | None:
        if not ref:
            return None
        rows = self.results.read_detail(ctx, ref, offset=0, limit=1)
        return rows[0] if rows else None


def _digest_body(name: str, report_type: str, summary: dict) -> str:
    lines = [name, f"Type: {report_type}", ""]
    for k in (
        "pass_rate_pct",
        "health_score_pct",
        "total_test_cases",
        "passed",
        "failed",
        "total_rules",
        "open_incidents",
        "tables_profiled",
    ):
        if k in summary:
            lines.append(f"{k.replace('_', ' ').title()}: {summary[k]}")
    return "\n".join(lines)


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)  # Decimal / datetime / etc. → string for JSON + UI


def _report_dict(rep: Report) -> dict:
    return {
        "id": rep.id,
        "name": rep.name,
        "report_type": rep.report_type,
        "project_id": rep.project_id,
        "spec": rep.spec,
        "status": rep.status,
        "summary": rep.summary,
        "latest_version": rep.latest_version,
        "schedule_cron": rep.schedule_cron,
        "generated_at": rep.generated_at.isoformat() if rep.generated_at else None,
        "created_at": rep.created_at.isoformat() if rep.created_at else None,
        "created_by": rep.created_by,
        "error": rep.error,
    }


def _version_dict(v: ReportVersion) -> dict:
    return {
        "id": v.id,
        "report_id": v.report_id,
        "version": v.version,
        "status": v.status,
        "summary": v.summary,
        "generated_at": v.generated_at.isoformat() if v.generated_at else None,
        "error": v.error,
    }


def _to_csv(report_type: str, doc: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    if report_type == "data_quality":
        w.writerow(
            ["rule", "table", "dimension", "severity", "passed", "metric_value", "threshold"]
        )
        for r in doc.get("rules", []):
            w.writerow(
                [
                    r.get("name"),
                    r.get("table_ref"),
                    r.get("dimension"),
                    r.get("severity"),
                    r.get("passed"),
                    r.get("metric_value"),
                    r.get("threshold"),
                ]
            )
    elif report_type == "executive":
        w.writerow(["metric", "value"])
        for k in (
            "health_score_pct",
            "validation_pass_rate_pct",
            "quality_pass_rate_pct",
            "open_incidents",
            "test_cases",
            "rules",
        ):
            w.writerow([k, doc.get(k)])
    elif report_type == "profile":
        w.writerow(
            ["table", "column", "type", "rows", "nulls", "empty", "unique", "approx", "min", "max"]
        )
        for t in doc.get("tables", []):
            for c in t.get("columns", []):
                w.writerow(
                    [
                        t.get("table"),
                        c.get("name"),
                        c.get("data_type"),
                        t.get("row_count"),
                        c.get("null_count"),
                        c.get("empty_count"),
                        c.get("distinct"),
                        c.get("distinct_approx"),
                        c.get("min"),
                        c.get("max"),
                    ]
                )
    elif doc.get("sign_off"):
        so = doc["sign_off"]
        w.writerow(["ran_by", so.get("ran_by"), "ran_at", so.get("ran_at")])
        w.writerow(["table", "result", "source_rows", "target_rows", "exception_keys"])
        for t in so.get("tables") or []:
            w.writerow(
                [
                    t.get("table"),
                    "match" if t.get("status") == "pass" else "fail",
                    t.get("src_count"),
                    t.get("dst_count"),
                    "; ".join(t.get("exception_keys") or []),
                ]
            )
    else:  # data_validation
        w.writerow(["test_case", "kind", "source", "target", "status", "engine"])
        for c in doc.get("test_cases", []):
            w.writerow(
                [
                    c.get("name"),
                    c.get("kind"),
                    c.get("source"),
                    c.get("target"),
                    c.get("status"),
                    c.get("engine"),
                ]
            )
    return buf.getvalue()


def _to_html(name: str, report_type: str, doc: dict) -> str:
    rows = ""
    intro = ""
    if report_type == "data_quality":
        head = "<tr><th>Rule</th><th>Table</th><th>Dimension</th><th>Passed</th></tr>"
        for r in doc.get("rules", []):
            rows += f"<tr><td>{_h(r.get('name'))}</td><td>{_h(r.get('table_ref'))}</td><td>{_h(r.get('dimension'))}</td><td>{_h(r.get('passed'))}</td></tr>"
    elif report_type == "executive":
        head = "<tr><th>Metric</th><th>Value</th></tr>"
        for k in (
            "health_score_pct",
            "validation_pass_rate_pct",
            "quality_pass_rate_pct",
            "open_incidents",
            "test_cases",
            "rules",
        ):
            rows += f"<tr><td>{k}</td><td>{_h(doc.get(k))}</td></tr>"
    elif doc.get("sign_off"):
        so = doc["sign_off"]
        head = "<tr><th>Table</th><th>Result</th><th>Source rows</th><th>Target rows</th><th>Exception keys</th></tr>"
        intro = f"<p class='k'>Ran by {_h(so.get('ran_by'))} · {_h(so.get('ran_at'))}</p>"
        for t in so.get("tables") or []:
            result = "match" if t.get("status") == "pass" else "fail"
            keys = "; ".join(t.get("exception_keys") or [])
            rows += (
                f"<tr><td>{_h(t.get('table'))}</td><td>{result}</td>"
                f"<td>{_h(t.get('src_count'))}</td><td>{_h(t.get('dst_count'))}</td>"
                f"<td>{_h(keys)}</td></tr>"
            )
    else:
        head = (
            "<tr><th>Test case</th><th>Kind</th><th>Source</th><th>Target</th><th>Status</th></tr>"
        )
        for c in doc.get("test_cases", []):
            rows += f"<tr><td>{_h(c.get('name'))}</td><td>{_h(c.get('kind'))}</td><td>{_h(c.get('source'))}</td><td>{_h(c.get('target'))}</td><td>{_h(c.get('status'))}</td></tr>"
    pr = doc.get("pass_rate_pct", doc.get("health_score_pct"))
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_h(name)}</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:2rem;color:#0f172a}"
        "table{border-collapse:collapse;width:100%;font-size:14px}"
        "th,td{border:1px solid #e2e8f0;padding:6px 10px;text-align:left}"
        "th{background:#f8fafc}h1{font-size:20px}.k{color:#64748b}</style></head><body>"
        f"<h1>{_h(name)}</h1>"
        f"<p class='k'>{_h(report_type)} · headline {_h(pr)}%</p>"
        f"{intro}"
        f"<table>{head}{rows}</table></body></html>"
    )


def _h(v: Any) -> str:
    return (
        str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if v is not None
        else ""
    )
