"""MonitorRunner — pushdown profiling + rule evaluation for a monitor.

`evaluate_rule` runs one rule's SQL and records its result/metric. `run_monitor`
checks every table in scope (MAX of the watermark, a metadata or COUNT(*) volume,
schema from the catalog) and evaluates every active rule. A full column profile is
`observe_table`, started on its own. This is the engine the scheduler drives.

Works against any connector that implements `execute()` — Snowflake in prod, the
in-memory `local` SQLite connector in dev/tests (no mocks).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from dvp.connectors.base import TableRef
from dvp.core.errors import NotFoundError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.models import Connection, DQRule, Monitor
from dvp.db.session import MetadataStore
from dvp.observability.evaluator import evaluate
from dvp.observability.observability_service import ProfilerService, RuleEvaluationService
from dvp.observability.service import RuleService, _rule_dict

_ACTIVE_STATES = {"enabled", "active"}


def _freshness_source(monitor: Monitor) -> str:
    """How this monitor measures freshness: watermark | file_arrival | auto."""
    from dvp.observability import alertpolicy

    return alertpolicy.freshness_source(getattr(monitor, "alert_policy", None))


class MonitorRunner:
    def __init__(self, store: MetadataStore, secrets=None) -> None:  # noqa: ANN001
        self.store = store
        self.secrets = secrets

    # ----- public -----
    def evaluate_rule(
        self,
        ctx: TenantContext,
        monitor_id: str,
        rule_id: str,
        table_ref: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            rule = RuleService._load_rule(s, ctx, monitor_id, rule_id)
            rule_d = _rule_dict(rule)
            monitor = s.get(Monitor, monitor_id)
            conn_info = self._conn_info(s, monitor)
            conn_info["secret"] = self._secret(ctx, conn_info)
            fqn = table_ref or rule_d.get("table_ref") or self._first_table(conn_info, monitor)
        ev = self._eval(conn_info, fqn, rule_d, now)
        rec = RuleEvaluationService(self.store).record_result(
            ctx,
            monitor_id,
            rule_id,
            {
                "table_ref": fqn,
                "metric_value": ev.metric_value,
                "passed": ev.passed,
                "observed": ev.observed,
                "ts": (now or datetime.now(UTC)).isoformat(),
            },
        )
        return rec

    def run_monitor(
        self,
        ctx: TenantContext,
        monitor_id: str,
        now: datetime | None = None,
        *,
        mode: str = "check",
    ) -> dict:
        ctx.require(Permission.WRITE)
        now = now or datetime.now(UTC)
        with self.store.session(ctx) as s:
            monitor = s.get(Monitor, monitor_id)
            if monitor is None or monitor.group_id != ctx.group_id:
                raise NotFoundError("monitor not found", details={"id": monitor_id})
            conn_info = self._conn_info(s, monitor)
            conn_info["secret"] = self._secret(ctx, conn_info)
            tables = monitor.scope.get("tables") or self._list_tables(conn_info)
            rules = s.scalars(
                select(DQRule).where(
                    DQRule.monitor_id == monitor_id,
                    DQRule.group_id == ctx.group_id,
                    DQRule.is_deleted == False,  # noqa: E712
                )
            ).all()
            rule_dicts = [_rule_dict(r) for r in rules]
            database = (
                monitor.scope.get("database") or conn_info["coordinates"].get("database") or ""
            )
            schema = monitor.scope.get("schema") or conn_info["coordinates"].get("schema") or ""
            wm = monitor.watermark_column
            fsource = _freshness_source(monitor)

        summary = {
            "monitor_id": monitor_id,
            "tables": [],
            "rules_evaluated": 0,
            "query_shape": "full_profile" if mode == "full" else "check",
        }
        prof = ProfilerService(self.store, self.secrets)
        for name in tables:
            fqn = ".".join([p for p in (database, schema, name) if p])
            observation = self._profile(conn_info, database, schema, name, wm, mode, fsource)
            prof.record_snapshot(ctx, monitor_id, fqn, observation)
            rcount = 0
            for rd in rule_dicts:
                if rd["state"] not in _ACTIVE_STATES:
                    continue
                if rd.get("table_ref") not in (None, fqn):
                    continue
                ev = self._eval(conn_info, fqn, rd, now)
                RuleEvaluationService(self.store).record_result(
                    ctx,
                    monitor_id,
                    rd["id"],
                    {
                        "table_ref": fqn,
                        "metric_value": ev.metric_value,
                        "passed": ev.passed,
                        "observed": ev.observed,
                        "ts": now.isoformat(),
                    },
                )
                rcount += 1
            summary["tables"].append(
                {
                    "table_ref": fqn,
                    "rules_evaluated": rcount,
                    "query_shape": observation.get("query_shape"),
                    "queries": observation.get("queries") or [],
                }
            )
            summary["rules_evaluated"] += rcount
        return summary

    # ----- internals -----
    # ----- sample-based rule generation + dry-run (no persistence) -----
    def _sample_rows(self, info: dict, fqn: str, wm: str | None, limit: int):  # noqa: ANN001
        """Pull up to `limit` rows for profiling/dry-run. With a watermark, take the most recent
        rows (the latest batch); otherwise the first N. Works for table / SQL / file sources."""
        connector = self._connector(info)
        database, schema, name = self._split(fqn, info)
        tref = TableRef(database=database, schema=schema, table=name)
        cols = list(connector.get_schema(tref))
        table_sql = connector.sql_ref(tref) if hasattr(connector, "sql_ref") else tref.fqn()
        # Order by the watermark only when THIS table actually has it — a monitor that spans a
        # source table (with a load column) and a curate table (without one) must not fail on the
        # latter; it just samples the first N rows instead.
        wm_here = wm if (wm and any(c.name.lower() == wm.lower() for c in cols)) else None
        order = f' ORDER BY "{wm_here.replace(chr(34), chr(34) * 2)}" DESC' if wm_here else ""
        base = f"SELECT * FROM {table_sql}{order}"
        sample = getattr(connector, "sample_sql", None)
        sql = sample(base, int(limit)) if callable(sample) else f"{base} LIMIT {int(limit)}"
        res = connector.execute(sql)
        rows = [dict(zip(res.columns, r, strict=False)) for r in res.rows]
        return cols, rows, tref

    def suggest_from_sample(self, ctx: TenantContext, monitor_id: str, *,
                            level: int = 2, sample_size: int = 10000) -> dict:
        """Profile a sample (latest watermark batch, else ~sample_size rows) and return SUGGESTED
        rules at the requested detail level (1/2/3). Nothing is saved — the caller reviews first."""
        ctx.require(Permission.WRITE)
        from dvp.observability.autogen import generate_rules
        from dvp.observability.sample_profile import build_profile

        sample_size = max(1, min(int(sample_size), 100000))
        with self.store.session(ctx) as s:
            monitor = s.get(Monitor, monitor_id)
            if monitor is None or monitor.group_id != ctx.group_id:
                raise NotFoundError("monitor not found", details={"id": monitor_id})
            info = self._conn_info(s, monitor)
            info["secret"] = self._secret(ctx, info)
            fqn = self._first_table(info, monitor)
            wm = monitor.watermark_column
        cols, rows, tref = self._sample_rows(info, fqn, wm, sample_size)
        profile = build_profile(tref.table, cols, rows, load_ts_column=wm)
        specs = generate_rules(profile, int(level))
        return {"level": int(level), "sample_size": sample_size, "sampled": len(rows),
                "watermark_used": bool(wm), "table_ref": fqn, "rules": specs}

    def dry_run_rules(self, ctx: TenantContext, monitor_id: str, rules: list[dict], *,
                      sample_size: int = 10000) -> dict:
        """Evaluate rule specs against the same sample WITHOUT persisting — pass/fail + examples."""
        ctx.require(Permission.READ)
        from dvp.observability.sample_profile import evaluate_rule_on_rows

        sample_size = max(1, min(int(sample_size), 100000))
        with self.store.session(ctx) as s:
            monitor = s.get(Monitor, monitor_id)
            if monitor is None or monitor.group_id != ctx.group_id:
                raise NotFoundError("monitor not found", details={"id": monitor_id})
            info = self._conn_info(s, monitor)
            info["secret"] = self._secret(ctx, info)
            fqn = self._first_table(info, monitor)
            wm = monitor.watermark_column
        _cols, rows, _tref = self._sample_rows(info, fqn, wm, sample_size)
        return {"sampled": len(rows), "watermark_used": bool(wm), "table_ref": fqn,
                "results": [evaluate_rule_on_rows(r, rows) for r in rules]}

    def generate_pack(self, ctx: TenantContext, monitor_id: str, *, tables: list[str] | None = None,
                      sample_size: int = 1000, level: int = 2) -> dict:
        """For each selected table in the monitor, profile a SAMPLE of the last batch (never a full
        scan) and build a suggested rule pack: column DQ rules (from the profile) + the SCD2
        load-aware rules when the table's resolved metadata is SCD2. Each spec is dry-run against the
        same sample so the user sees pass/fail before accepting. Nothing is persisted here — the
        endpoint creates them as state='suggested', origin='system'. `tables` (names, not FQNs)
        selects a subset; None = every table in scope."""
        ctx.require(Permission.WRITE)
        from dvp.observability.autogen import generate_rules
        from dvp.observability.sample_profile import build_profile, evaluate_rule_on_rows
        from dvp.services.table_metadata import build_scd2_rule_pack

        sample_size = max(1, min(int(sample_size), 100000))
        with self.store.session(ctx) as s:
            monitor = s.get(Monitor, monitor_id)
            if monitor is None or monitor.group_id != ctx.group_id:
                raise NotFoundError("monitor not found", details={"id": monitor_id})
            info = self._conn_info(s, monitor)
            info["secret"] = self._secret(ctx, info)
            scope = monitor.scope or {}
            wm = monitor.watermark_column
        scope_tables = scope.get("tables") or self._list_tables(info)
        chosen = [t for t in scope_tables if tables is None or t in set(tables)]
        database = scope.get("database") or info["coordinates"].get("database") or ""
        schema = scope.get("schema") or info["coordinates"].get("schema") or ""

        from dvp.services.table_metadata import TableMetadataService
        tms = TableMetadataService(self.store)
        out_tables: list[dict] = []
        for t in chosen:
            fqn = ".".join([p for p in (database, schema, t) if p])
            try:
                cols, rows, tref = self._sample_rows(info, fqn, wm, sample_size)
            except Exception as exc:  # noqa: BLE001 - surface per-table, don't abort the batch
                out_tables.append({"table_ref": fqn, "error": str(exc), "rules": []})
                continue
            profile = build_profile(tref.table, cols, rows, load_ts_column=wm)
            specs = list(generate_rules(profile, int(level)))
            meta = tms.resolve(ctx, fqn, None)
            if meta.get("load_pattern") == "scd2":
                specs += build_scd2_rule_pack(fqn, meta)["dq_rules"]
            enriched = []
            for sp in specs:
                sp = {**sp, "table_ref": fqn}
                dr = evaluate_rule_on_rows(sp, rows)
                enriched.append({**sp, "dry_run": {"applicable": dr["applicable"],
                                                    "passed": dr["passed"], "measured": dr["measured"]}})
            out_tables.append({"table_ref": fqn, "sampled": len(rows), "rules": enriched})
        return {"sample_size": sample_size, "level": int(level), "watermark_used": bool(wm),
                "tables_selected": len(chosen), "tables": out_tables}

    def _conn_info(self, s, monitor) -> dict:  # noqa: ANN001
        conn = s.get(Connection, monitor.connection_id)
        if conn is None:
            raise NotFoundError("connection not found", details={"id": monitor.connection_id})
        return {
            "source_type": conn.source_type,
            "coordinates": dict(conn.coordinates),
            "auth_method": conn.auth_method,
            "secret_ref": conn.secret_ref,
        }

    def _secret(self, ctx: TenantContext, info: dict) -> dict | None:
        ref = info.get("secret_ref")
        return self.secrets.get(ctx, ref) if (ref and self.secrets) else None

    def _connector(self, info: dict):  # noqa: ANN001
        from dvp.connectors.registry import build_connector

        return build_connector(
            info["source_type"], info["coordinates"], info["auth_method"], info.get("secret")
        )

    def _eval(self, info: dict, fqn: str, rule: dict, now: datetime | None):  # noqa: ANN001
        connector = self._connector(info)
        database, schema, name = self._split(fqn, info)
        tref = TableRef(database=database, schema=schema, table=name)
        # dialect is resolved from the connector itself — no source_type branching here.
        return evaluate(connector, tref, rule, now=now)

    def _profile(
        self,
        info: dict,
        database: str,
        schema: str,
        name: str,
        wm: str | None,
        mode: str,
        freshness_source: str = "auto",
    ) -> dict:
        from dvp.observability.observe import observe_check, observe_table

        connector = self._connector(info)
        tref = TableRef(database=database, schema=schema, table=name)
        if mode == "full":
            return observe_table(connector, tref, wm, freshness_source)
        return observe_check(connector, tref, wm, freshness_source)

    def _split(self, fqn: str, info: dict) -> tuple[str, str, str]:
        parts = fqn.split(".")
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        coords = info["coordinates"]
        return coords.get("database", ""), coords.get("schema", ""), parts[-1]

    def _first_table(self, info: dict, monitor) -> str:  # noqa: ANN001
        tables = (monitor.scope or {}).get("tables") or self._list_tables(info)
        if not tables:
            raise NotFoundError("no table to evaluate against")
        database = (
            (monitor.scope or {}).get("database") or info["coordinates"].get("database") or ""
        )
        schema = (monitor.scope or {}).get("schema") or info["coordinates"].get("schema") or ""
        return ".".join([p for p in (database, schema, tables[0]) if p])

    def _list_tables(self, info: dict) -> list[str]:
        connector = self._connector(info)
        return connector.list_tables() if hasattr(connector, "list_tables") else []
