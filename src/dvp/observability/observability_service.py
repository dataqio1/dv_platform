"""Table observability services.

ProfilerService (write path) captures a point-in-time snapshot of a table into the
`monitor_run` + `metric` tables using the pillar metric-name convention. It can take an
explicit observation (`record_snapshot`) — the path the real pushdown profiler will
also use — or compute one from the connection (`profile`).

TableObservabilityService (read path) assembles the per-table four-pillar cards, the
per-pillar history time series, and the column-level drill-down the UI renders.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from dvp.core.errors import NotFoundError, ValidationError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.base import utcnow
from dvp.db.models import Connection, DQRule, Metric, Monitor, MonitorRun, RuleResult, TableMetadata
from dvp.db.session import MetadataStore
from dvp.observability import alertpolicy
from dvp.observability import pillars as P
from dvp.observability.lineage import LineageService
from dvp.observability.service import RuleService, _rule_dict
from dvp.services import audit

RULE_METRIC_PREFIX = "rule."


def _merge_policy(base: dict, override: dict | None) -> dict:
    """Overlay a per-table alert-policy override onto the monitor default. Pillar sub-dicts
    are merged field-by-field so a table can tweak just one threshold (e.g. freshness error
    hours) while inheriting everything else from the monitor."""
    if not override:
        return base
    out = dict(base)
    for k, v in override.items():
        out[k] = {**out[k], **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _effective_policy(s, group_id: str, monitor, table_ref: str) -> dict:  # noqa: ANN001
    """The alert policy that applies to ONE table = monitor default + the table's override
    (stored in TableMetadata.config['alert_policy'])."""
    base = dict(getattr(monitor, "alert_policy", None) or {})
    tm = s.scalar(
        select(TableMetadata).where(
            TableMetadata.group_id == group_id, TableMetadata.table_ref == table_ref
        )
    )
    override = (tm.config or {}).get("alert_policy") if tm and isinstance(tm.config, dict) else None
    return _merge_policy(base, override if isinstance(override, dict) else None)


def _parse_ts(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            from dateutil import parser as _p

            dt = _p.parse(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _pct_deviation(series: list) -> float | None:
    """|%| the latest volume deviates from the median of its prior history, or None.

    Used to classify a volume anomaly's severity against the monitor's configured
    warn/severe percent thresholds (a plainer signal than the z-score for users)."""
    import statistics

    if len(series) < 2:
        return None
    latest = float(series[-1][1])
    prior = [float(v) for _ts, v in series[:-1] if v is not None]
    if not prior:
        return None
    median = statistics.median(prior)
    if median == 0:
        return None
    return abs(latest - median) / abs(median) * 100.0


def _weekday_volume_points(chron: list) -> list[dict]:  # noqa: ANN001
    """Volume history scored against the same weekday, so the chart band matches the incident."""
    from dvp.observability.anomaly import detect_weekday_volume

    points: list[dict] = []
    series: list[tuple] = []
    for mid, ts, v, _tags in chron:
        series.append((ts, v))
        pt = {"id": mid, "ts": ts.isoformat(), "value": v}
        if v is not None:
            res = detect_weekday_volume(series)
            pt.update(
                {
                    "baseline": res.median,
                    "lo": res.lo,
                    "hi": res.hi,
                    "anomaly": res.anomaly,
                    "direction": res.direction,
                    "score": round(res.score, 2),
                }
            )
        points.append(pt)
    return points


def _history_points_with_band(chron: list, values: list[float]) -> list[dict]:  # noqa: ANN001
    """Enrich each chronological point with the rolling baseline band + an anomaly flag, so
    the UI can overlay the expected band and mark unusual spikes/drops against the baseline."""
    from dvp.observability.anomaly import detect_anomaly

    points: list[dict] = []
    for i, (mid, ts, v, _tags) in enumerate(chron):
        pt = {"id": mid, "ts": ts.isoformat(), "value": v}
        if v is not None:
            # evaluate this point against the baseline learned from everything before it
            res = detect_anomaly(values[: i + 1], direction="both")
            pt.update(
                {
                    "baseline": res.median,
                    "lo": res.lo,
                    "hi": res.hi,
                    "anomaly": res.anomaly,
                    "direction": res.direction,
                    "score": round(res.score, 2),
                }
            )
        points.append(pt)
    return points


class ProfilerService:
    def __init__(self, store: MetadataStore, secrets=None) -> None:  # noqa: ANN001
        self.store = store
        self.secrets = secrets

    def record_snapshot(
        self, ctx: TenantContext, monitor_id: str, table_ref: str, observation: dict
    ) -> dict:
        """Persist one profiling snapshot. `observation` may carry: row_count,
        last_load_ts, columns (list of {name,...,null_rate,distinct_ratio}),
        quality_score, rows_analyzed, ts."""
        ctx.require(Permission.WRITE)
        now = _parse_ts(observation["ts"]) if observation.get("ts") else utcnow()
        with self.store.session(ctx) as s:
            monitor = self._load_monitor(s, ctx, monitor_id)
            # Per-table override merged over the monitor default (a table may set its own
            # freshness SLA, volume thresholds, etc.).
            policy = _effective_policy(s, ctx.group_id, monitor, table_ref)
            columns = observation.get("columns")
            col_names = [c["name"] for c in columns] if columns else None
            prior_cols = self._prior_columns(s, ctx, monitor_id, table_ref)
            schema_changes = (
                P.diff_columns(prior_cols, columns) if prior_cols is not None and columns else None
            )

            typed = (
                [
                    {"name": c["name"], "data_type": c.get("data_type") or c.get("type")}
                    for c in columns
                ]
                if columns
                else None
            )
            snap: dict = {"columns": typed} if typed is not None else {}
            if schema_changes is not None:
                snap["schema_changes"] = schema_changes
            if observation.get("freshness_gap"):
                snap["freshness_gap"] = str(observation["freshness_gap"])
            if observation.get("quality_gap"):
                snap["quality_gap"] = str(observation["quality_gap"])
            if observation.get("query_shape"):
                snap["query_shape"] = observation["query_shape"]
            if observation.get("queries"):
                snap["queries"] = list(observation["queries"])
            shape = observation.get("query_shape")
            if shape == "check":
                rows_analyzed = 0
            elif observation.get("rows_analyzed") is not None:
                rows_analyzed = int(observation["rows_analyzed"])
            else:
                rows_analyzed = int(observation.get("row_count") or 0)
            run = MonitorRun(
                org_id=ctx.org_id,
                group_id=ctx.group_id,
                monitor_id=monitor_id,
                table_ref=table_ref,
                status="succeeded",
                ruleset_snapshot=snap,
                rows_analyzed=rows_analyzed,
                query_shape=shape,
                started_at=now,
                finished_at=now,
            )
            s.add(run)
            s.flush()

            written: list[str] = []

            # ---- Freshness ----
            if observation.get("last_load_ts") is not None:
                last_load = _parse_ts(observation["last_load_ts"])
                lag = max(0.0, (now - last_load).total_seconds())
                self._metric(s, ctx, now, table_ref, monitor_id, P.M_FRESHNESS_LAG, lag)
                self._metric(
                    s,
                    ctx,
                    now,
                    table_ref,
                    monitor_id,
                    P.M_FRESHNESS_LAST_LOAD,
                    last_load.timestamp(),
                )
                written.append(P.Pillar.FRESHNESS.value)

            # ---- Volume ----
            if observation.get("row_count") is not None:
                self._metric(
                    s,
                    ctx,
                    now,
                    table_ref,
                    monitor_id,
                    P.M_VOLUME_ROWS,
                    float(observation["row_count"]),
                )
                written.append(P.Pillar.VOLUME.value)

            # ---- Schema (+ drift vs previous snapshot) ----
            if col_names is not None:
                self._metric(
                    s, ctx, now, table_ref, monitor_id, P.M_SCHEMA_COLS, float(len(col_names))
                )
                drift = P.change_count(schema_changes)
                self._metric(s, ctx, now, table_ref, monitor_id, P.M_SCHEMA_DRIFT, float(drift))
                written.append(P.Pillar.SCHEMA.value)

            # ---- Quality (table score + per-column stats) ----
            if observation.get("quality_score") is not None:
                self._metric(
                    s,
                    ctx,
                    now,
                    table_ref,
                    monitor_id,
                    P.M_QUALITY_SCORE,
                    float(observation["quality_score"]),
                )
                written.append(P.Pillar.QUALITY.value)
            for c in columns or []:
                if c.get("null_rate") is not None:
                    self._metric(
                        s,
                        ctx,
                        now,
                        table_ref,
                        monitor_id,
                        P.M_COL_NULL_RATE,
                        float(c["null_rate"]),
                        column=c["name"],
                    )
                if c.get("distinct_ratio") is not None:
                    self._metric(
                        s,
                        ctx,
                        now,
                        table_ref,
                        monitor_id,
                        P.M_COL_DISTINCT,
                        float(c["distinct_ratio"]),
                        column=c["name"],
                        approx=bool(c.get("distinct_approx")),
                    )

            audit.record(
                s,
                ctx,
                "table.snapshot",
                entity_type="monitor_run",
                entity_id=run.id,
                detail={"table": table_ref, "pillars": written},
            )
            result = {
                "run_id": run.id,
                "monitor_id": monitor_id,
                "table_ref": table_ref,
                "ts": now.isoformat(),
                "pillars_recorded": written,
                "watermark_column": monitor.watermark_column,
                "schema_changes": schema_changes,
                "query_shape": shape,
                "queries": list(observation.get("queries") or []),
            }
        # After the write commits: detect anomalies on the series + raise/resolve incidents.
        result["incidents"] = self._snapshot_incidents(
            ctx, monitor_id, table_ref, observation, schema_changes, policy
        )
        return result

    def _snapshot_incidents(  # noqa: ANN001
        self, ctx, monitor_id, table_ref, observation, schema_changes=None, policy=None
    ) -> list[dict]:
        """Volume anomaly + quality-floor incidents, with dedup + auto-resolve."""
        from dvp.observability.anomaly import detect_weekday_volume
        from dvp.observability.incidents import IncidentService

        inc = IncidentService(self.store)
        raised: list[dict] = []

        # Freshness pages only when a load time was actually measured and it is late.
        if observation.get("last_load_ts") is not None and not observation.get("freshness_gap"):
            last_load = _parse_ts(observation["last_load_ts"])
            observed_at = _parse_ts(observation["ts"]) if observation.get("ts") else utcnow()
            lag = max(0.0, (observed_at - last_load).total_seconds())
            key = f"freshness:{monitor_id}:{table_ref}"
            warn = alertpolicy.warn_after_seconds(policy)
            severity = alertpolicy.freshness_severity(lag, policy)
            file_based = observation.get("freshness_source") == "file_arrival"
            noun = "file arrived" if file_based else "Last load"
            if severity is not None:
                hours = lag / 3600
                raised.append(
                    inc.open_or_update(
                        ctx,
                        monitor_id=monitor_id,
                        table_ref=table_ref,
                        dedup_key=key,
                        kind="freshness",
                        dimension="freshness",
                        severity=severity,
                        summary=(
                            f"{noun} on {table_ref.split('.')[-1]} was {hours:.0f}h ago"
                        ),
                        evidence={
                            "metric": P.M_FRESHNESS_LAG,
                            "value": round(lag),
                            "threshold": round(warn),
                            "last_load_at": last_load.isoformat(),
                            "source": "file_arrival" if file_based else "watermark",
                        },
                    )
                )
            else:
                inc.resolve_by_key(ctx, key)

        # Volume anomaly (both spikes and drops matter for data volume).
        if observation.get("row_count") is not None:
            with self.store.session(ctx) as s:
                rows = s.execute(
                    select(Metric.ts, Metric.value, Metric.tags)
                    .where(
                        Metric.group_id == ctx.group_id,
                        Metric.table_ref == table_ref,
                        Metric.name == P.M_VOLUME_ROWS,
                        Metric.excluded.is_(False),
                    )
                    .order_by(Metric.ts.asc())
                ).all()
            series = [
                (ts, v)
                for ts, v, tags in rows
                if (tags or {}).get("monitor_id") == monitor_id
                and (tags or {}).get("column") is None
                and v is not None
            ]
            res = detect_weekday_volume(series)
            # Thin history must not open an incident and must not clear one from another weekday.
            if res.reason == "not enough history to alert":
                res = None
            key = f"anomaly:volume:{monitor_id}:{table_ref}:{series[-1][0].weekday()}" if series else ""
            if res is not None and res.anomaly:
                pct = _pct_deviation(series)
                sev = alertpolicy.volume_severity(pct, abs(res.score), policy)
                raised.append(
                    inc.open_or_update(
                        ctx,
                        monitor_id=monitor_id,
                        table_ref=table_ref,
                        dedup_key=key,
                        kind="anomaly",
                        dimension="volume",
                        severity=sev,
                        summary=f"Volume {res.direction} on {table_ref.split('.')[-1]}: {res.reason}",
                        evidence={"metric": P.M_VOLUME_ROWS, **res.as_dict()},
                    )
                )
            elif res is not None:
                inc.resolve_by_key(ctx, key)

        # Quality floor (table-level score).
        q = observation.get("quality_score")
        if q is not None:
            key = f"quality:{monitor_id}:{table_ref}"
            severity = alertpolicy.quality_severity(q, policy)
            if severity is not None:
                raised.append(
                    inc.open_or_update(
                        ctx,
                        monitor_id=monitor_id,
                        table_ref=table_ref,
                        dedup_key=key,
                        kind="quality",
                        dimension="quality",
                        severity=severity,
                        summary=f"Quality score {q:.0f}/100 on {table_ref.split('.')[-1]}",
                        evidence={
                            "metric": P.M_QUALITY_SCORE,
                            "value": q,
                            "threshold": alertpolicy.quality_floor(policy),
                        },
                    )
                )
            else:
                inc.resolve_by_key(ctx, key)

        # Schema drift names the columns. The first snapshot has nothing to compare.
        key = f"schema:{monitor_id}:{table_ref}"
        table_name = (table_ref or "").split(".")[-1] or table_ref
        if schema_changes is not None and P.change_count(schema_changes):
            summary = P.describe_schema_changes(schema_changes, table_name)
            raised.append(
                inc.open_or_update(
                    ctx,
                    monitor_id=monitor_id,
                    table_ref=table_ref,
                    dedup_key=key,
                    kind="schema_drift",
                    dimension="schema",
                    severity="warning",
                    summary=summary,
                    evidence={
                        "changes": schema_changes,
                        "also_used_by": self._also_used_by(ctx, monitor_id, table_ref),
                    },
                )
            )
        elif schema_changes is not None:
            inc.resolve_by_key(ctx, key)
        return raised

    def _also_used_by(self, ctx, monitor_id: str, table_ref: str) -> list[str]:  # noqa: ANN001
        """Other monitors and test cases that reference this table. One hop."""
        from dvp.db.models import CompareJob

        short = (table_ref or "").split(".")[-1]
        found: list[str] = []
        with self.store.session(ctx) as s:
            monitors = s.scalars(
                select(Monitor).where(
                    Monitor.group_id == ctx.group_id, Monitor.status != "deleted"
                )
            ).all()
            seen_monitors: set[str] = set()
            for monitor in monitors:
                if monitor.id == monitor_id or monitor.id in seen_monitors:
                    continue
                tables = list((monitor.scope or {}).get("tables") or [])
                if short in tables or table_ref in tables:
                    seen_monitors.add(monitor.id)
                    found.append(f"monitor {monitor.name}")
            rules = s.scalars(
                select(DQRule).where(
                    DQRule.group_id == ctx.group_id,
                    DQRule.is_deleted == False,  # noqa: E712
                    DQRule.monitor_id != monitor_id,
                )
            ).all()
            for rule in rules:
                ref = rule.table_ref or ""
                if rule.monitor_id in seen_monitors:
                    continue
                if ref == table_ref or ref.split(".")[-1] == short:
                    owner = next((m for m in monitors if m.id == rule.monitor_id), None)
                    label = f"monitor {owner.name}" if owner else f"rule {rule.name}"
                    if label not in found:
                        found.append(label)
                    seen_monitors.add(rule.monitor_id)
            jobs = s.scalars(
                select(CompareJob).where(CompareJob.group_id == ctx.group_id)
            ).all()
            for job in jobs:
                for side in (job.source or {}, job.target or {}):
                    table = str(side.get("table") or "")
                    if table == short or table == table_ref or table.endswith("." + short):
                        found.append(f"test case {job.name or job.kind}")
                        break
        return found

    def profile(self, ctx: TenantContext, monitor_id: str, table_ref: str) -> dict:
        """Compute an observation from the live connection and record it.

        Uses the connector's row count + schema; freshness comes from the monitor's
        watermark column where the connector can report its max value.
        """
        ctx.require(Permission.WRITE)
        from dvp.connectors.base import TableRef
        from dvp.connectors.registry import build_connector

        with self.store.session(ctx) as s:
            monitor = self._load_monitor(s, ctx, monitor_id)
            conn = s.get(Connection, monitor.connection_id)
            if conn is None:
                raise NotFoundError("connection not found", details={"id": monitor.connection_id})
            source_type, coordinates = conn.source_type, dict(conn.coordinates)
            auth_method, secret_ref = conn.auth_method, conn.secret_ref
            database = monitor.scope.get("database") or coordinates.get("database") or ""
            schema_name = monitor.scope.get("schema") or coordinates.get("schema") or ""
            wm_col, wm_type = monitor.watermark_column, monitor.watermark_type
            fsource = alertpolicy.freshness_source(_effective_policy(s, ctx.group_id, monitor, table_ref))

        secret = self.secrets.get(ctx, secret_ref) if (secret_ref and self.secrets) else None
        connector = build_connector(source_type, coordinates, auth_method, secret)
        ref = TableRef(database=database, schema=schema_name, table=table_ref.split(".")[-1])

        from dvp.observability.observe import observe_table

        observation = observe_table(connector, ref, wm_col, fsource)
        result = self.record_snapshot(ctx, monitor_id, table_ref, observation)
        result["watermark_type"] = wm_type
        return result

    # ----- helpers -----
    @staticmethod
    def _load_monitor(s, ctx, monitor_id):  # noqa: ANN001
        m = s.get(Monitor, monitor_id)
        if m is None or m.group_id != ctx.group_id:
            raise NotFoundError("monitor not found", details={"id": monitor_id})
        return m

    @staticmethod
    def _prior_columns(s, ctx, monitor_id, table_ref):  # noqa: ANN001
        run = s.scalars(
            select(MonitorRun)
            .where(
                MonitorRun.monitor_id == monitor_id,
                MonitorRun.group_id == ctx.group_id,
                MonitorRun.table_ref == table_ref,
            )
            .order_by(MonitorRun.started_at.desc())
        ).first()
        if run and isinstance(run.ruleset_snapshot, dict):
            return run.ruleset_snapshot.get("columns")
        return None

    @staticmethod
    def _metric(s, ctx, ts, table_ref, monitor_id, name, value, column=None, approx=False):  # noqa: ANN001
        tags = {"monitor_id": monitor_id}
        if column is not None:
            tags["column"] = column
        if approx:
            tags["approx"] = True  # the value came from an approximate (HLL) computation
        s.add(
            Metric(
                org_id=ctx.org_id,
                group_id=ctx.group_id,
                ts=ts,
                table_ref=table_ref,
                name=name,
                value=value,
                tags=tags,
            )
        )


class TableObservabilityService:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def list_tables(self, ctx: TenantContext, monitor_id: str) -> list[dict]:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            mon = ProfilerService._load_monitor(s, ctx, monitor_id)
            load_type = getattr(mon, "load_type", "incremental")
            # One grouped pass: distinct tables + total scans + last scan time.
            stats_rows = s.execute(
                select(
                    MonitorRun.table_ref,
                    func.count(MonitorRun.id),
                    func.max(MonitorRun.started_at),
                )
                .where(
                    MonitorRun.monitor_id == monitor_id,
                    MonitorRun.group_id == ctx.group_id,
                    MonitorRun.table_ref.is_not(None),
                )
                .group_by(MonitorRun.table_ref)
            ).all()
            out = []
            for table_ref, scan_count, last_scan in stats_rows:
                cards, overall, score, _last = self._assemble(s, ctx, monitor_id, table_ref)
                out.append(
                    {
                        "table_ref": table_ref,
                        "health": overall,
                        "score": score,
                        "pillars": {c["pillar"]: c["status"] for c in cards},
                        "scan_count": int(scan_count or 0),
                        "last_scanned": last_scan.isoformat() if last_scan else None,
                        "load_type": load_type,
                        "load_pattern": self._resolve_pattern(ctx, table_ref, load_type)[
                            "load_pattern"
                        ],
                    }
                )
            return out

    def _resolve_pattern(self, ctx: TenantContext, table_ref: str, load_type: str | None) -> dict:
        """Per-table load pattern (injected/auto/user record) overriding the monitor default."""
        from dvp.services.table_metadata import TableMetadataService

        return TableMetadataService(self.store).resolve(ctx, table_ref, load_type)

    def get_table_pillars(self, ctx: TenantContext, monitor_id: str, table_ref: str) -> dict:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            monitor = ProfilerService._load_monitor(s, ctx, monitor_id)
            cards, overall, score, last = self._assemble(s, ctx, monitor_id, table_ref)
            gaps = self._latest_gaps(s, ctx, monitor_id, table_ref)
            meta = self._resolve_pattern(ctx, table_ref, getattr(monitor, "load_type", None))
            suggested = None
            if not monitor.watermark_column:
                run = s.scalars(
                    select(MonitorRun)
                    .where(
                        MonitorRun.monitor_id == monitor_id,
                        MonitorRun.group_id == ctx.group_id,
                        MonitorRun.table_ref == table_ref,
                    )
                    .order_by(MonitorRun.started_at.desc())
                ).first()
                stored = (run.ruleset_snapshot or {}).get("columns", []) if run else []
                suggested = P.suggest_watermark(stored if isinstance(stored, list) else [])
                if suggested:
                    for card in cards:
                        if card["pillar"] == "freshness" and card["headline"] == "Not configured yet":
                            card["headline"] = f"{suggested['column']} looks like the load time"
                            card["detail"] = "Confirm it and the next check reads that column"
            return {
                "monitor_id": monitor_id,
                "table_ref": table_ref,
                "health": overall,
                "score": score,
                "last_profiled_at": last,
                "watermark_column": monitor.watermark_column,
                "suggested_watermark": suggested,
                "load_type": getattr(monitor, "load_type", "incremental"),
                "load_pattern": meta["load_pattern"],
                "load_pattern_source": meta.get("source"),
                "pattern_meta": meta,
                "pillars": cards,
                "query_shape": gaps.get("query_shape"),
                "queries": gaps.get("queries") or [],
                "lineage": LineageService(self.store).around(ctx, table_ref),
            }

    def set_point_excluded(self, ctx: TenantContext, point_id: str, excluded: bool) -> dict:
        """Exclude (or restore) a single observation point. Excluding removes it from the chart
        and from every baseline — the user's choice when a value is wrong (double load, missed
        load). History is kept, so it can be restored."""
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            m = s.get(Metric, point_id)
            if m is None or m.group_id != ctx.group_id:
                raise NotFoundError("data point not found", details={"id": point_id})
            m.excluded = bool(excluded)
            audit.record(
                s, ctx, "metric_point.exclude" if excluded else "metric_point.restore",
                entity_type="metric", entity_id=point_id,
                detail={"table_ref": m.table_ref, "metric": m.name, "ts": m.ts.isoformat() if m.ts else None},
            )
            return {"id": m.id, "excluded": m.excluded}

    def suggest_freshness(self, ctx: TenantContext, monitor_id: str, table_ref: str) -> dict:
        """Infer a freshness policy from this table's observed load times (the last_load metric
        history) — so the UI can auto-populate the deadline/cadence instead of guessing."""
        from dvp.observability import alertpolicy

        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            rows = self._metric_rows(s, ctx, monitor_id, table_ref, P.M_FRESHNESS_LAST_LOAD, limit=500)
        load_epochs = [float(v) for (_mid, _ts, v, _tags) in rows if v is not None]
        distinct = len({round(e) for e in load_epochs})
        suggestion = alertpolicy.infer_freshness(load_epochs)
        return {
            "suggestion": suggestion,
            "loads_observed": distinct,
            "enough_history": suggestion is not None,
            "message": (
                None if suggestion is not None
                else f"Need at least 3 observed loads to infer a schedule (have {distinct}). "
                "Let the monitor run a few cycles, then try again."
            ),
        }

    def get_pillar_history(
        self,
        ctx: TenantContext,
        monitor_id: str,
        table_ref: str,
        pillar: str,
        limit: int = 1000,
        since_days: int | None = None,
    ) -> dict:
        ctx.require(Permission.READ)
        try:
            pil = P.Pillar(pillar)
        except ValueError as exc:
            raise ValidationError(
                "unknown pillar", details={"pillar": pillar, "allowed": [p.value for p in P.Pillar]}
            ) from exc
        metric_name = P.PILLAR_PRIMARY_METRIC[pil]
        with self.store.session(ctx) as s:
            mon = ProfilerService._load_monitor(s, ctx, monitor_id)
            # since_days: None/0 = all history; N = trailing N-day window. The UI defaults the
            # window to the monitor's configured lookback (monitor.lookback_days, default 30).
            if since_days == 0:
                since_days = None
            rows = self._metric_rows(
                s, ctx, monitor_id, table_ref, metric_name, limit, since_days=since_days
            )
            # chronological (oldest → newest) for charting
            chron = list(reversed(rows))
            values = [float(v) for _id, _ts, v, _t in chron if v is not None]
            if pil == P.Pillar.VOLUME:
                points = _weekday_volume_points(chron)
            else:
                points = _history_points_with_band(chron, values)
            anomalies = sum(1 for p in points if p.get("anomaly"))
            latest = points[-1] if points else {}
            return {
                "monitor_id": monitor_id,
                "table_ref": table_ref,
                "pillar": pil.value,
                "metric": metric_name,
                "load_type": getattr(mon, "load_type", "incremental"),
                "points": points,
                "anomaly_count": anomalies,
                # the currently-expected band (for the "baseline" overlay + headline)
                "baseline": {
                    "median": latest.get("baseline"),
                    "lo": latest.get("lo"),
                    "hi": latest.get("hi"),
                },
            }

    def get_table_columns(self, ctx: TenantContext, monitor_id: str, table_ref: str) -> list[dict]:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            ProfilerService._load_monitor(s, ctx, monitor_id)
            # Latest schema snapshot's column list, if present.
            run = s.scalars(
                select(MonitorRun)
                .where(
                    MonitorRun.monitor_id == monitor_id,
                    MonitorRun.group_id == ctx.group_id,
                    MonitorRun.table_ref == table_ref,
                )
                .order_by(MonitorRun.started_at.desc())
            ).first()
            stored = (run.ruleset_snapshot or {}).get("columns", []) if run else []
            # Latest per-column metrics.
            latest = self._latest_column_metrics(s, ctx, monitor_id, table_ref)
            cols = []
            seen: set[str] = set()

            def _series(metric, col_name):  # noqa: ANN001,ANN202
                rows = self._metric_rows(s, ctx, monitor_id, table_ref, metric, 400, column=col_name)
                return [float(v) for _id, _ts, v, _t in rows if v is not None]

            def _median(xs):  # noqa: ANN001,ANN202
                sv = sorted(xs)
                n = len(sv)
                return sv[n // 2] if n % 2 else (sv[n // 2 - 1] + sv[n // 2]) / 2

            def _attention(col_name):  # noqa: ANN001,ANN202
                """Per-column 'needs a look' marker (so the UI doesn't make the user click every
                column). Returns (flag, kind, reason); the UI colors by kind. Kinds, in priority:
                  null_spike        — a null-rate spike well above the column's own baseline
                  cardinality_drift — distinct-ratio moved materially (a new/changed value mix)."""
                nvals = _series(P.M_COL_NULL_RATE, col_name)
                if len(nvals) >= 5:
                    med, mx = _median(nvals), max(nvals)
                    if mx >= 0.10 and mx >= med + 0.10:  # ≥10pp null-rate spike above baseline
                        return True, "null_spike", f"null-rate spiked to {mx * 100:.0f}% (baseline {med * 100:.0f}%)"
                dvals = _series(P.M_COL_DISTINCT, col_name)
                if len(dvals) >= 5:
                    dmed = _median(dvals)
                    if dmed > 0:
                        ext = max(dvals, key=lambda v: abs(v - dmed))
                        rel = abs(ext - dmed) / dmed
                        if rel >= 0.15 and abs(ext - dmed) > 1e-9:  # ≥15% distinct-ratio move
                            return True, "cardinality_drift", (
                                f"cardinality drift: distinct-ratio moved {rel * 100:.0f}% from baseline"
                            )
                return False, None, None

            for item in stored:
                name = item["name"] if isinstance(item, dict) else item
                seen.add(name)
                data_type = item.get("data_type") if isinstance(item, dict) else None
                stats = latest.get(name, {})
                attn, kind, reason = _attention(name)
                cols.append(
                    {
                        "column": name,
                        "data_type": data_type,
                        "null_rate": stats.get(P.M_COL_NULL_RATE),
                        "distinct_ratio": stats.get(P.M_COL_DISTINCT),
                        "distinct_approx": bool(stats.get("__distinct_approx")),
                        "attention": attn,
                        "attention_kind": kind,
                        "attention_reason": reason,
                    }
                )
            # Include any column that has metrics but isn't in the latest schema list.
            for name in latest:
                if name not in seen:
                    attn, kind, reason = _attention(name)
                    cols.append(
                        {
                            "column": name,
                            "null_rate": latest[name].get(P.M_COL_NULL_RATE),
                            "distinct_ratio": latest[name].get(P.M_COL_DISTINCT),
                            "distinct_approx": bool(latest[name].get("__distinct_approx")),
                            "attention": attn,
                            "attention_kind": kind,
                            "attention_reason": reason,
                        }
                    )
            return cols

    def get_column_history(
        self, ctx: TenantContext, monitor_id: str, table_ref: str, column: str, limit: int = 100
    ) -> dict:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            ProfilerService._load_monitor(s, ctx, monitor_id)
            series: dict[str, list[dict]] = {}
            for metric_name in (P.M_COL_NULL_RATE, P.M_COL_DISTINCT):
                rows = self._metric_rows(
                    s, ctx, monitor_id, table_ref, metric_name, limit, column=column
                )
                series[metric_name] = [
                    {"ts": ts.isoformat(), "value": v} for _id, ts, v, _t in reversed(rows)
                ]
            return {
                "monitor_id": monitor_id,
                "table_ref": table_ref,
                "column": column,
                "metrics": series,
            }

    # ----- internal -----
    def _assemble(self, s, ctx, monitor_id, table_ref):  # noqa: ANN001
        latest = self._latest_metrics(s, ctx, monitor_id, table_ref)
        vol_rows = self._metric_rows(s, ctx, monitor_id, table_ref, P.M_VOLUME_ROWS, limit=400)
        prior_volume = vol_rows[1][2] if len(vol_rows) > 1 else None
        from dvp.observability.anomaly import detect_weekday_volume

        weekday = detect_weekday_volume(
            [(ts, v) for _id, ts, v, _tags in reversed(vol_rows) if v is not None]
        ).as_dict()

        gaps = self._latest_gaps(s, ctx, monitor_id, table_ref)
        lag = latest.get(P.M_FRESHNESS_LAG)
        last_load = latest.get(P.M_FRESHNESS_LAST_LOAD)
        quality = latest.get(P.M_QUALITY_SCORE)
        # The latest scan is the truth. An older lag must not hide a watermark we could not read.
        if gaps.get("freshness_gap"):
            lag, last_load = None, None
        if gaps.get("quality_gap"):
            quality = None
        fresh_reason = gaps.get("freshness_gap")
        if not fresh_reason and lag is None and last_load is None:
            monitor = s.get(Monitor, monitor_id)
            watermark = (
                monitor.watermark_column
                if monitor is not None and monitor.group_id == ctx.group_id
                else None
            )
            if watermark:
                # The watermark is set. "Not configured yet" sends the owner back to setup.
                fresh_reason = f"No scan has read {watermark}"
        quality_reason = gaps.get("quality_gap")
        scanned = (
            latest.get(P.M_VOLUME_ROWS) is not None or latest.get(P.M_SCHEMA_COLS) is not None
        )
        if not quality_reason and quality is None and scanned:
            # A row count is not a quality score. "Not configured yet" sends them to setup.
            if gaps.get("query_shape") == "check":
                quality_reason = "The last check did not score columns"
            else:
                quality_reason = "No scan has scored quality"
        schema_card = P.schema_card(
            latest.get(P.M_SCHEMA_COLS),
            latest.get(P.M_SCHEMA_DRIFT),
            {},
            changes=gaps.get("schema_changes"),
            columns=gaps.get("columns"),
        )
        # Apply the monitor's per-kind schema severity policy (added/removed/type_changed).
        changes = gaps.get("schema_changes")
        if changes and schema_card.get("status") in ("warning", "critical"):
            from dvp.observability import alertpolicy

            mon = s.get(Monitor, monitor_id)
            policy = _effective_policy(s, ctx.group_id, mon, table_ref) if mon else {}
            sev = alertpolicy.schema_severity(policy, changes)
            if sev is None:  # every changed kind is set to "ignore"
                schema_card["status"] = P.Health.HEALTHY.value
                schema_card["detail"] = "schema changes ignored by policy"
            else:
                schema_card["status"] = P.Health.CRITICAL.value if sev == "critical" else P.Health.WARNING.value
        freshness_card = P.freshness_card(lag, last_load, {}, reason=fresh_reason)
        # Deadline-mode freshness: judge against the wall-clock deadline, not raw staleness.
        if last_load is not None:
            from dvp.observability import alertpolicy

            monp = s.get(Monitor, monitor_id)
            fpolicy = _effective_policy(s, ctx.group_id, monp, table_ref) if monp else {}
            if str(alertpolicy.resolve(fpolicy)["freshness"].get("mode")) == "deadline":
                status, detail = alertpolicy.freshness_status(lag, last_load, utcnow().timestamp(), fpolicy)
                freshness_card["status"] = status
                freshness_card["configured"] = True
                if detail:
                    freshness_card["detail"] = detail
        cards = [
            freshness_card,
            P.volume_card(latest.get(P.M_VOLUME_ROWS), prior_volume, {}, weekday=weekday),
            schema_card,
            P.quality_card(quality, {}, reason=quality_reason),
        ]
        overall = P.worst([P.Health(c["status"]) for c in cards]).value
        score = P.overall_score(cards)
        last = latest.get("__last_ts__")
        return cards, overall, score, last

    def _latest_gaps(self, s, ctx, monitor_id, table_ref) -> dict:  # noqa: ANN001
        run = s.scalars(
            select(MonitorRun)
            .where(
                MonitorRun.monitor_id == monitor_id,
                MonitorRun.group_id == ctx.group_id,
                MonitorRun.table_ref == table_ref,
            )
            .order_by(MonitorRun.started_at.desc())
        ).first()
        snap = run.ruleset_snapshot if run and isinstance(run.ruleset_snapshot, dict) else {}
        return {
            "freshness_gap": snap.get("freshness_gap"),
            "quality_gap": snap.get("quality_gap"),
            "schema_changes": snap.get("schema_changes"),
            "query_shape": snap.get("query_shape"),
            "queries": snap.get("queries") or [],
            "columns": snap.get("columns"),
        }

    def _latest_metrics(self, s, ctx, monitor_id, table_ref) -> dict:  # noqa: ANN001
        """Latest value per table-level metric name (ignores per-column metrics)."""
        rows = s.execute(
            select(Metric.name, Metric.value, Metric.ts, Metric.tags)
            .where(
                Metric.group_id == ctx.group_id,
                Metric.table_ref == table_ref,
            )
            .order_by(Metric.ts.asc())
        ).all()
        out: dict = {}
        last_ts = None
        for name, value, ts, tags in rows:
            if (tags or {}).get("monitor_id") != monitor_id:
                continue
            if (tags or {}).get("column") is not None:
                continue
            out[name] = value  # ascending order → ends on latest
            last_ts = ts.isoformat()
        if last_ts:
            out["__last_ts__"] = last_ts
        return out

    def _metric_rows(
        self, s, ctx, monitor_id, table_ref, metric_name, limit, column=None, since_days=None
    ):  # noqa: ANN001
        """Return up to `limit` rows (id, ts, value, tags) for one metric, newest first.
        User-excluded points are dropped so they don't skew the chart or baseline (the user's
        choice to remove a wrong value — e.g. a double load or a missed load).
        `since_days` restricts to the trailing window (None = all history)."""
        q = select(Metric.id, Metric.ts, Metric.value, Metric.tags).where(
            Metric.group_id == ctx.group_id,
            Metric.table_ref == table_ref,
            Metric.name == metric_name,
            Metric.excluded.is_(False),
        )
        if since_days:
            q = q.where(Metric.ts >= utcnow() - timedelta(days=int(since_days)))
        rows = s.execute(q.order_by(Metric.ts.desc())).all()
        filtered = []
        for mid, ts, value, tags in rows:
            if (tags or {}).get("monitor_id") != monitor_id:
                continue
            if (tags or {}).get("column") != column:  # exact match (None == table-level)
                continue
            filtered.append((mid, ts, value, tags))
            if len(filtered) >= limit:
                break
        return filtered

    def _latest_column_metrics(self, s, ctx, monitor_id, table_ref) -> dict:  # noqa: ANN001
        rows = s.execute(
            select(Metric.name, Metric.value, Metric.ts, Metric.tags)
            .where(
                Metric.group_id == ctx.group_id,
                Metric.table_ref == table_ref,
                Metric.name.in_([P.M_COL_NULL_RATE, P.M_COL_DISTINCT]),
            )
            .order_by(Metric.ts.asc())
        ).all()
        out: dict = {}
        for name, value, _ts, tags in rows:
            if (tags or {}).get("monitor_id") != monitor_id:
                continue
            col = (tags or {}).get("column")
            if col is None:
                continue
            out.setdefault(col, {})[name] = value
            if name == P.M_COL_DISTINCT and (tags or {}).get("approx"):
                out[col]["__distinct_approx"] = True
        return out


class RuleEvaluationService:
    """Capture and serve custom/built-in DQ rule results and their metrics.

    A rule evaluation (computed by the executor, pushdown SQL, or pushed in for testing)
    becomes a `rule_result` row plus a unified `metric` row named ``rule.<rule_id>`` so a
    rule's value is charted alongside the pillar metrics (docs/design/10 §5).
    """

    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def record_result(
        self, ctx: TenantContext, monitor_id: str, rule_id: str, result: dict
    ) -> dict:
        ctx.require(Permission.WRITE)
        now = _parse_ts(result["ts"]) if result.get("ts") else utcnow()
        with self.store.session(ctx) as s:
            rule = RuleService._load_rule(s, ctx, monitor_id, rule_id)
            table_ref = result.get("table_ref") or rule.table_ref
            metric_value = result.get("metric_value")
            threshold = result.get("threshold")
            if threshold is None:
                threshold = (rule.config or {}).get("threshold")
            passed = result.get("passed")
            if passed is None:
                # Derive: violation-count style (pass when value <= threshold).
                if metric_value is not None and threshold is not None:
                    passed = float(metric_value) <= float(threshold)
                else:
                    passed = True

            rr = RuleResult(
                monitor_run_id=result.get("run_id"),
                dq_rule_id=rule_id,
                rule_version=rule.version,
                passed=bool(passed),
                metric_value=metric_value,
                threshold=threshold,
                observed=result.get("observed") or {},
                table_ref=table_ref,
                bad_sample_ref=result.get("bad_sample_ref"),
                created_at=now,
            )
            s.add(rr)

            if metric_value is not None:
                s.add(
                    Metric(
                        org_id=ctx.org_id,
                        group_id=ctx.group_id,
                        ts=now,
                        table_ref=table_ref,
                        name=f"{RULE_METRIC_PREFIX}{rule_id}",
                        value=float(metric_value),
                        tags={
                            "monitor_id": monitor_id,
                            "rule_id": rule_id,
                            "rule_name": rule.name,
                            "severity": rule.severity,
                            "dimension": (rule.config or {}).get("dimension", "quality"),
                            "table_ref": table_ref,
                        },
                    )
                )
            audit.record(
                s,
                ctx,
                "rule.result",
                entity_type="dq_rule",
                entity_id=rule_id,
                detail={"passed": bool(passed), "value": metric_value, "table": table_ref},
            )
            rule_name = rule.name
            rule_severity = rule.severity
            rule_dim = (rule.config or {}).get("dimension", "quality")
            observed = rr.observed or {}
            result = {
                "rule_id": rule_id,
                "monitor_id": monitor_id,
                "table_ref": table_ref,
                "passed": bool(passed),
                "metric_value": metric_value,
                "threshold": threshold,
                "ts": now.isoformat(),
                "failing_keys": [] if passed else list(observed.get("failing_keys") or []),
                "sample_sql": None if passed else observed.get("sample_sql"),
            }
        # A failing rule opens an incident (deduped); a passing one resolves it.
        from dvp.observability.incidents import IncidentService

        inc = IncidentService(self.store)
        key = f"rule:{rule_id}"
        if not bool(passed):
            inc.open_or_update(
                ctx,
                monitor_id=monitor_id,
                table_ref=table_ref,
                dedup_key=key,
                kind="rule",
                dimension=rule_dim,
                severity=rule_severity,
                dq_rule_id=rule_id,
                summary=f"Rule '{rule_name}' failed on {(table_ref or '').split('.')[-1]}: "
                f"value {metric_value} vs threshold {threshold}",
                evidence={
                    "rule_id": rule_id,
                    "rule_name": rule_name,
                    "value": metric_value,
                    "threshold": threshold,
                    "failing_keys": result["failing_keys"],
                    "sample_sql": result["sample_sql"],
                },
            )
        else:
            inc.resolve_by_key(ctx, key)
        return result

    def history(self, ctx: TenantContext, monitor_id: str, rule_id: str, limit: int = 100) -> dict:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            RuleService._load_rule(s, ctx, monitor_id, rule_id)
            rows = s.scalars(
                select(RuleResult)
                .where(RuleResult.dq_rule_id == rule_id)
                .order_by(RuleResult.created_at.desc())
                .limit(limit)
            ).all()
            points = [
                {
                    "ts": r.created_at.isoformat(),
                    "value": r.metric_value,
                    "passed": r.passed,
                    "threshold": r.threshold,
                }
                for r in reversed(rows)  # chronological
            ]
            return {"monitor_id": monitor_id, "rule_id": rule_id, "points": points}

    def rules_for_table(self, ctx: TenantContext, monitor_id: str, table_ref: str) -> list[dict]:
        """Rules bound to this table (or monitor-wide) with their latest result."""
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            ProfilerService._load_monitor(s, ctx, monitor_id)
            rules = s.scalars(
                select(DQRule).where(
                    DQRule.monitor_id == monitor_id,
                    DQRule.group_id == ctx.group_id,
                    DQRule.is_deleted == False,  # noqa: E712
                    or_(DQRule.table_ref == table_ref, DQRule.table_ref.is_(None)),
                )
            ).all()
            out = []
            for rule in rules:
                latest = s.scalars(
                    select(RuleResult)
                    .where(RuleResult.dq_rule_id == rule.id)
                    .order_by(RuleResult.created_at.desc())
                    .limit(1)
                ).first()
                d = _rule_dict(rule)
                d["latest"] = (
                    {
                        "passed": latest.passed,
                        "metric_value": latest.metric_value,
                        "threshold": latest.threshold,
                        "ts": latest.created_at.isoformat(),
                        "failing_keys": []
                        if latest.passed
                        else list((latest.observed or {}).get("failing_keys") or []),
                        "sample_sql": None
                        if latest.passed
                        else (latest.observed or {}).get("sample_sql"),
                    }
                    if latest
                    else None
                )
                out.append(d)
            return out
