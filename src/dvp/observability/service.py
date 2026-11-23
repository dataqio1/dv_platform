"""MonitorService + RuleService — CRUD for monitors and table-level DQ rules.

Rule edits bump `version` (runtime-edit safety, docs/design/04 §3.5); deletes are
soft so historical run results stay interpretable.
"""

from __future__ import annotations

from sqlalchemy import func, select

from dvp.core.cron import is_valid as cron_is_valid
from dvp.core.cron import next_due
from dvp.core.errors import NotFoundError, ValidationError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.base import utcnow
from dvp.db.models import Connection, DQRule, Incident, Monitor, MonitorRun
from dvp.db.session import MetadataStore
from dvp.services import audit

SCHEDULES = {
    "hourly": "0 * * * *",
    "daily": "0 6 * * *",
    "manual": None,
}


def resolve_schedule(schedule: str | None, schedule_cron: str | None) -> str | None:
    if schedule is None:
        return schedule_cron
    if schedule not in SCHEDULES:
        raise ValidationError(
            "unknown schedule", details={"schedule": schedule, "allowed": sorted(SCHEDULES)}
        )
    return SCHEDULES[schedule]


_DOW = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def schedule_label(cron: str | None) -> str:
    """Friendly description of a schedule cron for the common picker patterns; falls back to
    the raw expression for anything exotic."""
    if not cron:
        return "manual"
    if cron == SCHEDULES["hourly"]:
        return "Every hour"
    parts = cron.split()
    if len(parts) != 5:
        return cron
    minute, hour, dom, month, dow = parts
    # Every N hours: "0 */N * * *"
    if minute.isdigit() and hour.startswith("*/") and dom == "*" and month == "*" and dow == "*":
        n = hour[2:]
        return f"Every {n} hours" if n.isdigit() else cron
    # Single minute+hour → a daily/weekly/monthly at a fixed time (unpadded hour, e.g. "6:00").
    if minute.isdigit() and hour.isdigit():
        hhmm = f"{int(hour)}:{int(minute):02d} UTC"
        if dom == "*" and month == "*" and dow == "*":
            return f"Daily at {hhmm}"
        if dom == "*" and month == "*" and dow.isdigit():
            return f"Weekly on {_DOW[int(dow) % 7]} at {hhmm}"
        if dom.isdigit() and month == "*" and dow == "*":
            return f"Monthly on the {_ordinal(int(dom))} at {hhmm}"
    return cron


class MonitorService:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def create(
        self,
        ctx: TenantContext,
        *,
        connection_id: str,
        name: str,
        scope: dict,
        description: str | None = None,
        tags: list[str] | None = None,
        schedule_cron: str | None = None,
        watermark_column: str | None = None,
        watermark_type: str | None = None,
        load_type: str = "incremental",
        lookback_days: int = 30,
        alert_policy: dict | None = None,
        project_id: str | None = None,
        source_system: str | None = None,
        layer: str | None = None,
        metadata_columns: dict | None = None,
        metadata_convention_ids: list[str] | None = None,
    ) -> dict:
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            # A monitor inherits its connection's ETL metadata-column conventions (named + inline)
            # unless it declares its own (docs/design/11 §3.4).
            if metadata_columns is None or metadata_convention_ids is None:
                conn = s.get(Connection, connection_id)
                if metadata_columns is None:
                    metadata_columns = conn.metadata_columns if conn else None
                if metadata_convention_ids is None:
                    metadata_convention_ids = conn.metadata_convention_ids if conn else None
            m = Monitor(
                org_id=ctx.org_id,
                group_id=ctx.group_id,
                connection_id=connection_id,
                name=name,
                description=description,
                tags=tags or [],
                scope=scope,
                schedule_cron=schedule_cron,
                watermark_column=watermark_column,
                watermark_type=watermark_type,
                load_type=load_type or "incremental",
                lookback_days=int(lookback_days or 30),
                alert_policy=dict(alert_policy or {}),
                project_id=project_id,
                source_system=source_system,
                layer=layer,
                metadata_columns=metadata_columns or None,
                metadata_convention_ids=metadata_convention_ids or None,
                created_by=ctx.user_id,
            )
            s.add(m)
            s.flush()
            audit.record(
                s,
                ctx,
                "monitor.create",
                entity_type="monitor",
                entity_id=m.id,
                detail={
                    "name": name,
                    "description": description,
                    "connection_id": connection_id,
                    "scope": scope,
                },
            )
            return _monitor_dict(m)

    def update(self, ctx: TenantContext, monitor_id: str, fields: dict) -> dict:
        """Rename and/or rewrite the description. Unchanged fields are not audited."""
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            m = self._load(s, ctx, monitor_id)
            changes: dict = {}
            if "name" in fields:
                name = (fields["name"] or "").strip()
                if not name:
                    raise ValidationError("monitor name is required")
                if len(name) > 255:
                    raise ValidationError("monitor name is too long", details={"max": 255})
                if name != m.name:
                    changes["name"] = {"from": m.name, "to": name}
                    m.name = name
            if "description" in fields:
                raw = fields["description"]
                description = (raw or "").strip() or None
                if description and len(description) > 2000:
                    raise ValidationError("description is too long", details={"max": 2000})
                if description != m.description:
                    changes["description"] = {"from": m.description, "to": description}
                    m.description = description
            if "schedule_cron" in fields or "schedule" in fields:
                if "schedule_cron" in fields:
                    raw = (fields.get("schedule_cron") or "").strip()
                    cron = raw or None  # "" / null → Manual
                    if cron is not None and not cron_is_valid(cron):
                        raise ValidationError("invalid schedule", details={"schedule_cron": cron})
                else:
                    cron = resolve_schedule(fields["schedule"], None)
                if cron != m.schedule_cron:
                    changes["schedule"] = {
                        "from": schedule_label(m.schedule_cron),
                        "to": schedule_label(cron),
                    }
                    m.schedule_cron = cron
            if fields.get("lookback_days") is not None:
                lb = int(fields["lookback_days"])
                if lb != m.lookback_days:
                    changes["lookback_days"] = {"from": m.lookback_days, "to": lb}
                    m.lookback_days = lb
            if fields.get("alert_policy") is not None:
                new_policy = dict(fields["alert_policy"] or {})
                if new_policy != (m.alert_policy or {}):
                    changes["alert_policy"] = {"from": m.alert_policy or {}, "to": new_policy}
                    m.alert_policy = new_policy
            if changes:
                audit.record(
                    s,
                    ctx,
                    "monitor.update",
                    entity_type="monitor",
                    entity_id=m.id,
                    detail={"changes": changes},
                )
            return _monitor_dict(m)

    def remove(self, ctx: TenantContext, monitor_id: str) -> dict:
        """Take the monitor off the active list. The row and its scan history stay
        so the audit record still points at a real object."""
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            m = self._load(s, ctx, monitor_id)
            snapshot = {
                "name": m.name,
                "description": m.description,
                "connection_id": m.connection_id,
                "scope": m.scope,
                "status": m.status,
            }
            m.status = "deleted"
            audit.record(
                s,
                ctx,
                "monitor.delete",
                entity_type="monitor",
                entity_id=m.id,
                detail=snapshot,
            )
            return {"id": m.id, "status": "deleted", "name": snapshot["name"]}

    def assign(
        self,
        ctx: TenantContext,
        monitor_id: str,
        *,
        project_id: str | None = None,
        source_system: str | None = None,
        layer: str | None = None,
        tags: list | None = None,
    ) -> dict:
        """Set this DQ test case's project / grouping taxonomy / tags."""
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            m = self._load(s, ctx, monitor_id)
            changes: dict = {}
            if project_id is not None and (project_id or None) != m.project_id:
                changes["project_id"] = {"from": m.project_id, "to": project_id or None}
                m.project_id = project_id or None
            if source_system is not None and (source_system or None) != m.source_system:
                changes["source_system"] = {"from": m.source_system, "to": source_system or None}
                m.source_system = source_system or None
            if layer is not None and (layer or None) != m.layer:
                changes["layer"] = {"from": m.layer, "to": layer or None}
                m.layer = layer or None
            if tags is not None and list(tags) != list(m.tags or []):
                changes["tags"] = {"from": list(m.tags or []), "to": list(tags)}
                m.tags = tags
            if changes:
                audit.record(
                    s,
                    ctx,
                    "monitor.assign",
                    entity_type="monitor",
                    entity_id=m.id,
                    detail={"changes": changes},
                )
            return _monitor_dict(m)

    def list(self, ctx: TenantContext) -> list[dict]:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            rows = s.scalars(
                select(Monitor).where(
                    Monitor.group_id == ctx.group_id, Monitor.status != "deleted"
                )
            ).all()
            lasts = _last_runs(s, ctx.group_id, [m.id for m in rows])
            return [_monitor_dict(m, last_run=lasts.get(m.id)) for m in rows]

    def get(self, ctx: TenantContext, monitor_id: str) -> dict:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            m = self._load(s, ctx, monitor_id)
            return _monitor_dict(m, last_run=_last_runs(s, ctx.group_id, [m.id]).get(m.id))

    def summary(self, ctx: TenantContext) -> dict:
        """Dashboard aggregate for the monitors list screen: per-monitor health derived from
        active incidents, status-chip counts, and active/silenced incident totals. The UI's
        4 KPI cards and status filter chips read straight from this."""
        ctx.require(Permission.READ)
        now = utcnow()
        with self.store.session(ctx) as s:
            monitors = s.scalars(
                select(Monitor).where(
                    Monitor.group_id == ctx.group_id, Monitor.status != "deleted"
                )
            ).all()
            active_incidents = s.scalars(
                select(Incident).where(
                    Incident.group_id == ctx.group_id,
                    Incident.status.in_(("open", "acknowledged")),
                )
            ).all()

        worst_by_monitor: dict[str, int] = {}
        rank = {"warning": 1, "error": 2, "critical": 2}
        open_cnt = ack_cnt = silenced_cnt = 0
        for inc in active_incidents:
            if inc.status == "open":
                open_cnt += 1
            else:
                ack_cnt += 1
            if inc.silenced_until is not None and inc.silenced_until > now:
                silenced_cnt += 1
            if inc.monitor_id:
                sev = rank.get((inc.severity or "warning").lower(), 1)
                worst_by_monitor[inc.monitor_id] = max(worst_by_monitor.get(inc.monitor_id, 0), sev)

        health = {"healthy": 0, "warning": 0, "error": 0, "paused": 0}
        by_status: dict[str, int] = {}
        for m in monitors:
            by_status[m.status] = by_status.get(m.status, 0) + 1
            if m.status != "active":
                health["paused"] += 1
                continue
            worst = worst_by_monitor.get(m.id, 0)
            health["error" if worst >= 2 else "warning" if worst == 1 else "healthy"] += 1

        total = len(monitors)
        monitored = health["healthy"] + health["warning"] + health["error"]
        return {
            "total": total,
            "health": health,
            "by_status": by_status,
            "incidents": {
                "open": open_cnt,
                "acknowledged": ack_cnt,
                "active_total": open_cnt + ack_cnt,
                "silenced": silenced_cnt,
            },
            "monitors_with_active_incidents": len(worst_by_monitor),
            "coverage_pct": round(100.0 * health["healthy"] / monitored, 1) if monitored else 0.0,
        }

    def set_status(self, ctx: TenantContext, monitor_id: str, status: str) -> dict:
        ctx.require(Permission.WRITE)
        if status == "deleted":
            raise ValidationError("remove the monitor to delete it")
        with self.store.session(ctx) as s:
            m = self._load(s, ctx, monitor_id)
            if m.status == status:
                return _monitor_dict(m)
            previous = m.status
            m.status = status
            audit.record(
                s,
                ctx,
                "monitor.status",
                entity_type="monitor",
                entity_id=m.id,
                detail={"changes": {"status": {"from": previous, "to": status}}},
            )
            return _monitor_dict(m)

    def set_watermark(
        self, ctx: TenantContext, monitor_id: str, column: str | None, wtype: str | None
    ) -> dict:
        """Set/clear the monitor-level watermark column (Edit Monitor → Watermark)."""
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            m = self._load(s, ctx, monitor_id)
            if m.watermark_column == column and m.watermark_type == wtype:
                return _monitor_dict(m)
            changes = {
                "watermark_column": {"from": m.watermark_column, "to": column},
                "watermark_type": {"from": m.watermark_type, "to": wtype},
            }
            m.watermark_column = column
            m.watermark_type = wtype
            audit.record(
                s,
                ctx,
                "monitor.watermark",
                entity_type="monitor",
                entity_id=m.id,
                detail={"changes": changes},
            )
            return _monitor_dict(m)

    _LOAD_TYPES = ("incremental", "full_reload", "append")

    def set_load_type(self, ctx: TenantContext, monitor_id: str, load_type: str) -> dict:
        """Set how the table is loaded (incremental | full_reload | append)."""
        ctx.require(Permission.WRITE)
        if load_type not in self._LOAD_TYPES:
            raise ValidationError(
                "unknown load_type", details={"load_type": load_type, "allowed": self._LOAD_TYPES}
            )
        with self.store.session(ctx) as s:
            m = self._load(s, ctx, monitor_id)
            if m.load_type == load_type:
                return _monitor_dict(m)
            previous = m.load_type
            m.load_type = load_type
            audit.record(
                s,
                ctx,
                "monitor.load_type",
                entity_type="monitor",
                entity_id=m.id,
                detail={"changes": {"load_type": {"from": previous, "to": load_type}}},
            )
            return _monitor_dict(m)

    @staticmethod
    def _load(s, ctx: TenantContext, monitor_id: str) -> Monitor:  # noqa: ANN001
        m = s.get(Monitor, monitor_id)
        if m is None or m.group_id != ctx.group_id or m.status == "deleted":
            raise NotFoundError("monitor not found", details={"id": monitor_id})
        return m


class RuleService:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def create(self, ctx: TenantContext, monitor_id: str, spec: dict) -> dict:
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            MonitorService._load(s, ctx, monitor_id)
            rule = DQRule(
                org_id=ctx.org_id,
                group_id=ctx.group_id,
                monitor_id=monitor_id,
                name=spec["name"],
                description=spec.get("description"),
                type=spec["type"],
                columns=spec.get("columns", []),
                config=spec.get("config", {}),
                severity=spec.get("severity", "warning"),
                scope=spec.get("scope", "incremental"),
                state=spec.get("state", "enabled"),
                origin=spec.get("origin", "user"),
                confidence=spec.get("confidence"),
                table_ref=spec.get("table_ref"),
                created_by=ctx.user_id,
            )
            s.add(rule)
            s.flush()
            audit.record(s, ctx, "rule.create", entity_type="dq_rule", entity_id=rule.id)
            return _rule_dict(rule)

    def list(self, ctx: TenantContext, monitor_id: str) -> list[dict]:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            rows = s.scalars(
                select(DQRule).where(
                    DQRule.monitor_id == monitor_id,
                    DQRule.group_id == ctx.group_id,
                    DQRule.is_deleted == False,  # noqa: E712
                )
            ).all()
            return [_rule_dict(r) for r in rows]

    def autogenerate_load_aware(self, ctx: TenantContext, monitor_id: str, table_ref: str) -> dict:
        """Materialize the load-aware DQ rule pack for a table from its resolved load-type metadata
        (docs/design/11 §6). All generated rules are labelled origin='system'. Idempotent: a rule
        of the same (table_ref, type) that already exists is skipped — never overwriting a user's
        edit (re-run only adds what's new). SCD2 is the first pack; other patterns no-op for now."""
        ctx.require(Permission.WRITE)
        from dvp.observability.metrics import MetricService
        from dvp.services.table_metadata import TableMetadataService, build_scd2_rule_pack

        with self.store.session(ctx) as s:
            m = MonitorService._load(s, ctx, monitor_id)
            connection_id = m.connection_id
        meta = TableMetadataService(self.store).resolve(ctx, table_ref, None)
        if meta.get("load_pattern") != "scd2":
            return {"created": [], "skipped": [], "metrics": [],
                    "note": f"no load-aware pack for load_pattern '{meta.get('load_pattern')}' yet"}

        pack = build_scd2_rule_pack(table_ref, meta)
        existing = {(r.get("table_ref"), r["type"]) for r in self.list(ctx, monitor_id)}
        created, skipped = [], []
        for sp in pack["dq_rules"]:
            if (sp.get("table_ref"), sp["type"]) in existing:
                skipped.append(sp["name"])
                continue
            created.append(self.create(ctx, monitor_id, sp))

        # Active-record count baseline → the metric engine (gets history/charts). Idempotent by name.
        metrics_out = []
        msvc = MetricService(self.store)
        have_metrics = {md["name"] for md in msvc.list(ctx, monitor_id=monitor_id)}
        for msp in pack["metrics"]:
            if msp["name"] in have_metrics:
                continue
            md = msvc.create(
                ctx, name=msp["name"], source_type="sql", connection_id=connection_id,
                source_config={"query": msp["query"], "value_column": "v"},
                monitor_id=monitor_id, description=msp.get("description"),
            )
            rule = msvc.add_rule(
                ctx, md["id"], name="active-count deviation", type=msp["rule"]["type"],
                config=msp["rule"]["config"], severity=msp["rule"]["severity"], origin="system",
            )
            metrics_out.append({"metric": md, "rule": rule})
        return {"created": created, "skipped": skipped, "metrics": metrics_out}

    def update(self, ctx: TenantContext, monitor_id: str, rule_id: str, patch: dict) -> dict:
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            rule = self._load_rule(s, ctx, monitor_id, rule_id)
            old_version = rule.version
            content_edit = any(
                f in patch and patch[f] is not None
                for f in ("name", "description", "config", "severity", "scope", "columns")
            )
            for field in ("name", "description", "config", "severity", "scope", "state", "columns"):
                if field in patch and patch[field] is not None:
                    setattr(rule, field, patch[field])
            # A person editing an automated/suggested rule takes ownership of it, so a later
            # re-detection won't silently overwrite their decision (docs/design/11 §6.2). A pure
            # enable/disable (state-only) is not a content edit and keeps the original provenance.
            if "origin" in patch and patch["origin"]:
                rule.origin = patch["origin"]
            elif content_edit and rule.origin in ("system", "suggested"):
                rule.origin = "user"
            rule.version = old_version + 1  # runtime-edit safety
            rule.updated_by = ctx.user_id
            audit.record(
                s,
                ctx,
                "rule.update",
                entity_type="dq_rule",
                entity_id=rule.id,
                detail={"from_version": old_version, "to_version": rule.version},
            )
            return _rule_dict(rule)

    def delete(self, ctx: TenantContext, monitor_id: str, rule_id: str) -> None:
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            rule = self._load_rule(s, ctx, monitor_id, rule_id)
            rule.is_deleted = True  # soft delete
            audit.record(s, ctx, "rule.delete", entity_type="dq_rule", entity_id=rule.id)

    @staticmethod
    def _load_rule(s, ctx: TenantContext, monitor_id: str, rule_id: str) -> DQRule:  # noqa: ANN001
        rule = s.get(DQRule, rule_id)
        if (
            rule is None
            or rule.group_id != ctx.group_id
            or rule.monitor_id != monitor_id
            or rule.is_deleted
        ):
            raise NotFoundError("rule not found", details={"id": rule_id})
        return rule


def _last_runs(s, group_id: str, monitor_ids: list[str]) -> dict:  # noqa: ANN001
    if not monitor_ids:
        return {}
    rows = s.execute(
        select(MonitorRun.monitor_id, func.max(MonitorRun.finished_at))
        .where(MonitorRun.group_id == group_id, MonitorRun.monitor_id.in_(monitor_ids))
        .group_by(MonitorRun.monitor_id)
    ).all()
    return {mid: ts for mid, ts in rows}


def _resolved_alert_policy(stored: dict | None) -> dict:
    """The monitor's alert policy merged with engine defaults, so the UI always gets full values."""
    from dvp.observability import alertpolicy

    return alertpolicy.resolve(stored)


def _monitor_dict(m: Monitor, last_run=None) -> dict:  # noqa: ANN001
    nxt = next_due(m.schedule_cron, utcnow()) if m.schedule_cron else None
    return {
        "id": m.id,
        "connection_id": m.connection_id,
        "name": m.name,
        "description": m.description,
        "tags": m.tags,
        "scope": m.scope,
        "schedule_cron": m.schedule_cron,
        "schedule_label": schedule_label(m.schedule_cron),
        "next_run": nxt.isoformat() if nxt else None,
        "last_run_at": last_run.isoformat() if last_run else None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "status": m.status,
        "watermark_column": m.watermark_column,
        "watermark_type": m.watermark_type,
        "load_type": m.load_type,
        "lookback_days": m.lookback_days,
        "alert_policy": _resolved_alert_policy(m.alert_policy),
        "project_id": m.project_id,
        "source_system": m.source_system,
        "layer": m.layer,
        "metadata_columns": m.metadata_columns or None,
        "metadata_convention_ids": m.metadata_convention_ids or [],
    }


def _rule_dict(r: DQRule) -> dict:
    return {
        "id": r.id,
        "monitor_id": r.monitor_id,
        "name": r.name,
        "description": r.description,
        "type": r.type,
        "table_ref": r.table_ref,
        "columns": r.columns,
        "config": r.config,
        "dimension": (r.config or {}).get("dimension", "quality"),
        "severity": r.severity,
        "scope": r.scope,
        "state": r.state,
        "origin": r.origin,
        "confidence": r.confidence,
        "version": r.version,
    }
