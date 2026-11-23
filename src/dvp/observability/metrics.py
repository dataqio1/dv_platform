"""MetricService — historical-baseline metrics: definitions, rules, capture, and evaluation.

A metric captures a numeric value every run (Phase 1: from SQL on an existing connection),
stores it as permanent history, and evaluates its baseline rules (deviation / monotonic /
growth) against prior valid history via the pure `baseline` evaluator. Tenant-scoped, gated,
audited. The metric result is the worst across its rules (FR-R1).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, time
from decimal import Decimal

from sqlalchemy import select

from dvp.abstractions.secret_store import SecretStore
from dvp.core.errors import NotFoundError, ValidationError
from dvp.core.logging import get_logger
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.base import utcnow
from dvp.db.models import Connection, MetricDefinition, MetricObservation, MetricRule
from dvp.db.session import MetadataStore
from dvp.observability import baseline
from dvp.services import audit

log = get_logger("dvp.metrics")

_SOURCE_TYPES = ("sql", "file", "dashboard")
# Metric result = worst across rules (FR-R1). Higher rank is worse.
_RANK = {"pass": 0, "learning": 1, "warn": 2, "fail": 3, "error": 4}


def _worse(a: str, b: str) -> str:
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


class MetricService:
    def __init__(self, store: MetadataStore, secrets: SecretStore | None = None) -> None:
        self.store = store
        self.secrets = secrets

    # ----------------------------- metric definitions -----------------------------
    def create(
        self, ctx: TenantContext, *, name: str, source_type: str, source_config: dict,
        connection_id: str | None = None, description: str | None = None,
        monitor_id: str | None = None, project_id: str | None = None,
        fallback_threshold: float | None = None, retention_days: int = 400, timezone: str = "UTC",
        schedule_cron: str | None = None,
    ) -> dict:
        ctx.require(Permission.WRITE)
        if source_type not in _SOURCE_TYPES:
            raise ValidationError("unknown source_type", details={"allowed": _SOURCE_TYPES})
        if not (name or "").strip():
            raise ValidationError("name is required")
        if source_type == "sql" and not connection_id:
            raise ValidationError("sql metrics require a connection_id")
        with self.store.session(ctx) as s:
            md = MetricDefinition(
                org_id=ctx.org_id, group_id=ctx.group_id, name=name.strip(),
                description=description, source_type=source_type, source_config=source_config or {},
                connection_id=connection_id, timezone=timezone, retention_days=retention_days,
                fallback_threshold=fallback_threshold, monitor_id=monitor_id, project_id=project_id,
                schedule_cron=schedule_cron, created_by=ctx.user_id,
            )
            s.add(md)
            s.flush()
            audit.record(s, ctx, "metric.create", entity_type="metric", entity_id=md.id,
                         detail={"name": md.name, "source": source_type})
            return _md_dict(md)

    def list(self, ctx: TenantContext, *, monitor_id: str | None = None, project_id: str | None = None) -> list[dict]:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            q = select(MetricDefinition).where(MetricDefinition.group_id == ctx.group_id)
            if monitor_id:
                q = q.where(MetricDefinition.monitor_id == monitor_id)
            if project_id:
                q = q.where(MetricDefinition.project_id == project_id)
            return [_md_dict(m) for m in s.scalars(q.order_by(MetricDefinition.name)).all()]

    def get(self, ctx: TenantContext, metric_id: str) -> dict:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            md = self._load(s, ctx, metric_id)
            rules = s.scalars(
                select(MetricRule).where(MetricRule.metric_def_id == metric_id)
            ).all()
            return {**_md_dict(md), "rules": [_rule_dict(r) for r in rules]}

    def delete(self, ctx: TenantContext, metric_id: str) -> None:
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            md = self._load(s, ctx, metric_id)
            for r in s.scalars(select(MetricRule).where(MetricRule.metric_def_id == metric_id)).all():
                s.delete(r)
            for o in s.scalars(select(MetricObservation).where(MetricObservation.metric_def_id == metric_id)).all():
                s.delete(o)
            audit.record(s, ctx, "metric.delete", entity_type="metric", entity_id=metric_id)
            s.delete(md)

    def create_from_table_suggestions(
        self, ctx: TenantContext, *, monitor_id: str, table_ref: str, connection_id: str,
        load_pattern: str, selections: list[dict],
    ) -> dict:
        """Materialize user-acknowledged load-pattern rule suggestions: create a row-count metric
        for the table and attach the chosen baseline rules (with any tweaked params). `selections`
        is [{key, params:{...}}] — only the suggestions the user ticked, with their edited numbers."""
        ctx.require(Permission.WRITE)
        from dvp.services.table_metadata import rule_suggestions

        catalog = {s["key"]: s for s in rule_suggestions(load_pattern)}
        chosen = [(sel, catalog[sel["key"]]) for sel in selections if sel.get("key") in catalog]
        if not chosen:
            raise ValidationError("no valid suggestions selected",
                                  details={"load_pattern": load_pattern})

        # One row-count metric per table drives all its baseline rules.
        table = table_ref.split(".")[-1]
        query = f'SELECT COUNT(*) AS v FROM {table_ref}'
        md = self.create(
            ctx, name=f"{table} row count", source_type="sql", connection_id=connection_id,
            source_config={"query": query, "value_column": "v"}, monitor_id=monitor_id,
            description=f"Auto-created from the {load_pattern} load-pattern suggestions for {table_ref}",
        )
        rules = []
        for sel, spec in chosen:
            cfg = {**(spec.get("config") or {})}
            for p in spec.get("params", []):
                key = p["key"]
                cfg[key] = sel.get("params", {}).get(key, p.get("default"))
            rules.append(self.add_rule(
                ctx, md["id"], name=spec["label"], type=spec["type"], config=cfg,
                severity=sel.get("severity", spec.get("severity", "warn")),
            ))
        return {"metric": md, "rules": rules}

    # ----------------------------- rules -----------------------------
    def add_rule(self, ctx: TenantContext, metric_id: str, *, name: str, type: str, config: dict, severity: str = "warn", origin: str = "user") -> dict:
        ctx.require(Permission.WRITE)
        if type not in baseline.BASELINE_RULE_TYPES:
            raise ValidationError(
                "unsupported rule type (Phase 1)",
                details={"type": type, "allowed": sorted(baseline.BASELINE_RULE_TYPES)},
            )
        with self.store.session(ctx) as s:
            self._load(s, ctx, metric_id)
            r = MetricRule(
                org_id=ctx.org_id, group_id=ctx.group_id, metric_def_id=metric_id,
                name=name or type, type=type, config=config or {}, severity=severity,
                origin=origin, created_by=ctx.user_id,
            )
            s.add(r)
            s.flush()
            audit.record(s, ctx, "metric.rule.add", entity_type="metric_rule", entity_id=r.id,
                         detail={"metric_id": metric_id, "type": type})
            return _rule_dict(r)

    def delete_rule(self, ctx: TenantContext, rule_id: str) -> None:
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            r = s.scalar(select(MetricRule).where(MetricRule.id == rule_id, MetricRule.group_id == ctx.group_id))
            if r is None:
                raise NotFoundError("rule not found", details={"id": rule_id})
            audit.record(s, ctx, "metric.rule.delete", entity_type="metric_rule", entity_id=rule_id)
            s.delete(r)

    # ----------------------------- run (capture + evaluate) -----------------------------
    def run(self, ctx: TenantContext, metric_id: str, *, run_id: str | None = None, run_date: str | None = None, business_date=None) -> dict:  # noqa: ANN001
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            md = self._load(s, ctx, metric_id)
            md_snapshot = _md_dict(md)
            reset_iso = md.reset_at.isoformat() if md.reset_at else None
            fallback = md.fallback_threshold
            rules = [
                _rule_dict(r)
                for r in s.scalars(
                    select(MetricRule).where(
                        MetricRule.metric_def_id == metric_id, MetricRule.enabled
                    )
                ).all()
            ]

        captured = self._capture(ctx, md_snapshot, run_date)
        out = []
        for dim_key, value in captured:
            history = self._history(ctx, metric_id, dim_key)
            rule_results: list[dict] = []
            if value is None:
                status = baseline.ERROR
                rule_results = [{"status": baseline.ERROR, "message": "value could not be captured"}]
            else:
                status = baseline.PASS
                for rule in rules:
                    cfg = {**(rule["config"] or {}), "severity": rule["severity"],
                           "reset": reset_iso, "fallback_threshold": fallback}
                    res = baseline.evaluate(rule["type"], value, history, cfg)
                    rule_results.append({"rule_id": rule["id"], "name": rule["name"],
                                         "type": rule["type"], **res.as_dict()})
                    status = _worse(status, res.status)
            source_ref = self._source_ref(md_snapshot, run_date)
            self._store(ctx, metric_id, dim_key, value, status, run_id, business_date, source_ref, rule_results)
            # A breached baseline rule opens/updates an incident → flows through the notification
            # dispatcher (email/Slack/Teams/webhook). A Pass/Learning clears it; an Error (couldn't
            # capture) is NOT a data-quality failure (FR-S6), so it never opens a DQ incident.
            self._raise_or_resolve(ctx, md_snapshot, dim_key, status, value, rule_results)
            out.append({"dimension_key": dim_key, "value": None if value is None else float(value),
                        "status": status, "rules": rule_results})
        with self.store.session(ctx) as s:
            md = s.get(MetricDefinition, metric_id)
            if md is not None:
                md.last_run_at = utcnow()
            audit.record(s, ctx, "metric.run", entity_type="metric", entity_id=metric_id,
                         detail={"run_id": run_id, "observations": len(out)})
        return {"metric_id": metric_id, "run_id": run_id, "observations": out}

    def run_due(self, ctx: TenantContext, now=None) -> int:  # noqa: ANN001
        """Run metrics whose cron schedule has come due since their last run (driven by the
        scheduler). Manual metrics (no schedule) are never run here."""
        from datetime import UTC, datetime

        from dvp.core.cron import previous_due

        now = now or datetime.now(UTC)
        with self.store.session(ctx) as s:
            mds = s.scalars(
                select(MetricDefinition).where(
                    MetricDefinition.group_id == ctx.group_id,
                    MetricDefinition.schedule_cron.is_not(None),
                )
            ).all()
            due = []
            for md in mds:
                slot = previous_due(md.schedule_cron or "", now)
                last = md.last_run_at
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                if slot is not None and (last is None or last < slot):
                    due.append(md.id)
        ran = 0
        for mid in due:
            try:
                self.run(ctx, mid, run_id=f"sched-{now.isoformat()}")
                ran += 1
            except Exception:  # noqa: BLE001 - one bad metric must not stop the rest
                log.warning(
                    "scheduled_metric_run_failed", metric_id=mid, group_id=ctx.group_id, exc_info=True
                )
        return ran

    def _raise_or_resolve(self, ctx, md, dim_key, status, value, rule_results):  # noqa: ANN001
        from dvp.observability.incidents import IncidentService

        inc = IncidentService(self.store)
        key = f"metric:{md['id']}" + (f":{dim_key}" if dim_key else "")
        if status in (baseline.FAIL, baseline.WARN):
            breached = next((r for r in rule_results if r.get("status") in (baseline.FAIL, baseline.WARN)), {})
            summary = breached.get("message") or f"{md['name']} failed a baseline rule"
            inc.open_or_update(
                ctx, monitor_id=md.get("monitor_id"), dedup_key=key, kind="baseline",
                dimension="quality", severity="critical" if status == baseline.FAIL else "warning",
                summary=f"{md['name']}: {summary}",
                evidence={"metric": md["name"], "value": None if value is None else float(value),
                          "baseline": breached.get("baseline_value"), "limit": breached.get("limit")},
            )
        elif status == baseline.PASS:
            inc.resolve_by_key(ctx, key)

    # ----------------------------- history + lifecycle -----------------------------
    def observations(self, ctx: TenantContext, metric_id: str, *, dimension_key: str | None = None, limit: int = 200) -> list[dict]:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            self._load(s, ctx, metric_id)
            q = select(MetricObservation).where(
                MetricObservation.metric_def_id == metric_id, MetricObservation.group_id == ctx.group_id
            )
            if dimension_key is not None:
                q = q.where(MetricObservation.dimension_key == dimension_key)
            rows = s.scalars(q.order_by(MetricObservation.captured_at.desc()).limit(limit)).all()
            return [_obs_dict(o) for o in rows]

    def exclude(self, ctx: TenantContext, observation_id: str, reason: str) -> dict:
        return self._set_flags(ctx, observation_id, "metric.observation.exclude",
                               excluded=True, exclude_reason=reason)

    def accept(self, ctx: TenantContext, observation_id: str) -> dict:
        return self._set_flags(ctx, observation_id, "metric.observation.accept", accepted=True)

    def reset(self, ctx: TenantContext, metric_id: str, *, reset_at, reason: str | None = None) -> dict:  # noqa: ANN001
        ctx.require(Permission.WRITE)
        cutoff = baseline._reset_cutoff(reset_at)
        if cutoff is None:
            raise ValidationError("reset_at must be an ISO date/datetime")
        with self.store.session(ctx) as s:
            md = self._load(s, ctx, metric_id)
            md.reset_at = cutoff if isinstance(cutoff, datetime) else datetime.combine(cutoff, time())
            audit.record(s, ctx, "metric.reset", entity_type="metric", entity_id=metric_id,
                         detail={"reset_at": str(reset_at), "reason": reason})
            return _md_dict(md)

    # ----------------------------- helpers -----------------------------
    def _load(self, s, ctx: TenantContext, metric_id: str) -> MetricDefinition:  # noqa: ANN001
        md = s.scalar(
            select(MetricDefinition).where(
                MetricDefinition.id == metric_id, MetricDefinition.group_id == ctx.group_id
            )
        )
        if md is None:
            raise NotFoundError("metric not found", details={"id": metric_id})
        return md

    def _set_flags(self, ctx, observation_id, action, **flags):  # noqa: ANN001
        # Only editors+ can exclude/accept (NFR-6) — WRITE covers editor/group_admin.
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            o = s.scalar(select(MetricObservation).where(
                MetricObservation.id == observation_id, MetricObservation.group_id == ctx.group_id))
            if o is None:
                raise NotFoundError("observation not found", details={"id": observation_id})
            for k, v in flags.items():
                setattr(o, k, v)
            audit.record(s, ctx, action, entity_type="metric_observation", entity_id=observation_id)
            return _obs_dict(o)

    def _history(self, ctx: TenantContext, metric_id: str, dim_key: str | None) -> list[baseline.Observation]:
        with self.store.session(ctx) as s:
            q = select(MetricObservation).where(
                MetricObservation.metric_def_id == metric_id,
                MetricObservation.group_id == ctx.group_id,
            )
            q = q.where(MetricObservation.dimension_key.is_(None) if dim_key is None
                        else MetricObservation.dimension_key == dim_key)
            rows = s.scalars(q.order_by(MetricObservation.captured_at.asc())).all()
            return [
                baseline.Observation(
                    value=o.value, ts=o.captured_at, business_date=o.business_date,
                    status=o.status, excluded=o.excluded, accepted=o.accepted,
                )
                for o in rows
            ]

    def _store(self, ctx, metric_id, dim_key, value, status, run_id, business_date, source_ref, detail):  # noqa: ANN001
        with self.store.session(ctx) as s:
            existing = None
            if run_id:  # dedup by run_id + dimension (FR-H5)
                existing = s.scalar(select(MetricObservation).where(
                    MetricObservation.metric_def_id == metric_id,
                    MetricObservation.group_id == ctx.group_id,
                    MetricObservation.run_id == run_id,
                    (MetricObservation.dimension_key.is_(None) if dim_key is None
                     else MetricObservation.dimension_key == dim_key),
                ))
            dval = None if value is None else Decimal(str(value))
            if existing is not None:
                existing.value = dval
                existing.status = status
                existing.source_ref = source_ref
                existing.detail = {"rules": detail}
                existing.captured_at = utcnow()
                return
            s.add(MetricObservation(
                org_id=ctx.org_id, group_id=ctx.group_id, metric_def_id=metric_id,
                dimension_key=dim_key, value=dval, captured_at=utcnow(),
                business_date=business_date, run_id=run_id, status=status,
                source_ref=source_ref, detail={"rules": detail},
            ))

    def _capture(self, ctx: TenantContext, md: dict, run_date: str | None) -> list[tuple[str | None, object]]:
        """Capture value(s) for the metric. SQL = run the query; File = generate the extraction
        SQL over the file's loaded table (FileConnector speaks SQL over CSV/JSON/Parquet)."""
        if md["source_type"] == "sql":
            query = (md.get("source_config") or {}).get("query")
            if not query:
                raise ValidationError("sql metric requires source_config.query")
            return self._run_capture_sql(ctx, md, query, run_date)
        if md["source_type"] == "file":
            return self._run_capture_sql(ctx, md, self._file_query(md.get("source_config") or {}), run_date)
        raise ValidationError(
            "only SQL and file metrics are supported in this phase",
            details={"source_type": md["source_type"]},
        )

    def _run_capture_sql(self, ctx, md, query, run_date):  # noqa: ANN001
        connector = self._connector(ctx, md["connection_id"])
        cfg = md.get("source_config") or {}
        q = query.replace("{{run_date}}", str(run_date) if run_date else "")
        try:
            res = connector.execute(q)
        except Exception:  # noqa: BLE001 - capture failure → Error status (FR-S6), not a crash
            return [(None, None)]
        cols = [str(c).lower() for c in res.columns]
        value_col = str(cfg.get("value_column") or "").lower()
        dim_cols = [str(c).lower() for c in (cfg.get("dimension_columns") or [])]
        vi = cols.index(value_col) if value_col in cols else (len(cols) - 1 if cols else 0)
        dim_idx = [cols.index(d) for d in dim_cols if d in cols]
        out: list[tuple[str | None, object]] = []
        for row in res.rows:
            dk = ".".join(str(row[i]) for i in dim_idx) if dim_idx else None
            out.append((dk, row[vi] if vi < len(row) else None))
        return out or [(None, None)]

    def _file_query(self, cfg: dict) -> str:
        """Build the extraction SQL for a file metric. extraction.method ∈
        row_count | sum | avg | min | max | distinct (on extraction.column for the aggregates).
        The FileConnector exposes the file as a SQL table named source_config.table."""
        table = cfg.get("table")
        if not table:
            raise ValidationError("file metric requires source_config.table")
        ext = cfg.get("extraction") or {"method": "row_count"}
        method = str(ext.get("method", "row_count")).lower()
        col = ext.get("column")
        tbl = f'"{table}"'
        if method == "row_count":
            agg = "COUNT(*)"
        elif method in ("sum", "avg", "min", "max"):
            if not col:
                raise ValidationError(f"file extraction '{method}' requires a column")
            agg = f'{method.upper()}("{col}")'
        elif method == "distinct":
            if not col:
                raise ValidationError("file extraction 'distinct' requires a column")
            agg = f'COUNT(DISTINCT "{col}")'
        else:
            raise ValidationError(
                "unsupported file extraction method",
                details={"method": method, "allowed": ["row_count", "sum", "avg", "min", "max", "distinct"]},
            )
        dim_cols = cfg.get("dimension_columns") or []
        if dim_cols:
            dims = ", ".join(f'"{d}"' for d in dim_cols)
            return f"SELECT {dims}, {agg} AS v FROM {tbl} GROUP BY {dims}"
        return f"SELECT {agg} AS v FROM {tbl}"

    def _connector(self, ctx: TenantContext, connection_id: str | None):
        from dvp.connectors.registry import build_connector

        if not connection_id:
            raise ValidationError("metric has no connection")
        with self.store.session(ctx) as s:
            conn = s.get(Connection, connection_id)
            if conn is None or conn.group_id != ctx.group_id:
                raise ValidationError("connection not found", details={"id": connection_id})
            source_type, coords = conn.source_type, dict(conn.coordinates)
            auth, secret_ref = conn.auth_method, conn.secret_ref
        secret = self.secrets.get(ctx, secret_ref) if (secret_ref and self.secrets) else None
        return build_connector(source_type, coords, auth, secret)

    def _source_ref(self, md: dict, run_date: str | None) -> dict:
        cfg = md.get("source_config") or {}
        q = (cfg.get("query") or "").replace("{{run_date}}", str(run_date) if run_date else "")
        return {"query_hash": hashlib.sha256(q.encode()).hexdigest()[:16], "source_type": md["source_type"]}


# ----------------------------- serializers -----------------------------
def _md_dict(m: MetricDefinition) -> dict:
    return {
        "id": m.id, "name": m.name, "description": m.description, "source_type": m.source_type,
        "source_config": m.source_config or {}, "connection_id": m.connection_id,
        "timezone": m.timezone, "retention_days": m.retention_days,
        "fallback_threshold": m.fallback_threshold,
        "reset_at": m.reset_at.isoformat() if m.reset_at else None,
        "schedule_cron": m.schedule_cron,
        "last_run_at": m.last_run_at.isoformat() if m.last_run_at else None,
        "monitor_id": m.monitor_id, "project_id": m.project_id,
    }


def _rule_dict(r: MetricRule) -> dict:
    return {"id": r.id, "metric_def_id": r.metric_def_id, "name": r.name, "type": r.type,
            "config": r.config or {}, "severity": r.severity, "enabled": r.enabled,
            "origin": r.origin}


def _obs_dict(o: MetricObservation) -> dict:
    return {
        "id": o.id, "dimension_key": o.dimension_key,
        "value": None if o.value is None else float(o.value),
        "captured_at": o.captured_at.isoformat() if o.captured_at else None,
        "business_date": o.business_date.isoformat() if o.business_date else None,
        "run_id": o.run_id, "status": o.status, "excluded": o.excluded,
        "exclude_reason": o.exclude_reason, "accepted": o.accepted, "detail": o.detail or {},
    }
