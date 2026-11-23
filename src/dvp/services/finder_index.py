"""FinderIndexService — builds & refreshes the Find Column (Value Locator) index.

For each column of each in-scope table it computes a Bloom filter over the column's values
and stores it in `finder_column_profile`. A later search tests the stored filters to shortlist
candidate columns (instant, no source scan) and confirms the survivors with a live query.

Design choices that keep this correct and cheap:
  * The indexer reads `CAST(col AS TEXT)` for every column, so the Bloom contains exactly the
    text form the search's `CAST(col AS TEXT) = 'value'` predicate compares against — the Bloom
    never produces a false negative relative to the confirm query.
  * Bloom filters are mergeable, so an incremental scan (rows past a watermark) builds a delta
    filter with the stored (m, k) and OR-merges it in — no full re-read.
  * Everything is per-table defensive: a table that can't be read is skipped with a warning.
"""

from __future__ import annotations

from dvp.abstractions.secret_store import SecretStore
from dvp.connectors.base import TableRef
from dvp.connectors.dialect import count_distinct_sql
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.base import utcnow
from dvp.db.models import FinderColumnProfile, FinderScanConfig, FinderScanRun
from dvp.db.session import MetadataStore
from dvp.services.bloom import BloomFilter
from dvp.services.column_profile import (
    ColumnStats,
    IndexPolicy,
    classify_column,
    is_binary_type,
    is_date_type,
)
from dvp.services.finder import FinderService, _limited, _lit, _qcol, _text

MAX_ROWS_PER_TABLE = 2_000_000
DEFAULT_FP = 0.01
CHUNK_COLUMNS = 25  # columns per UNION-ALL distinct batch (fewer round-trips than one query/column)
MAX_TEXT_LEN = 100  # skip columns whose values average longer than this (large free-text / blobs)


class FinderIndexService:
    def __init__(self, store: MetadataStore, secrets: SecretStore) -> None:
        self.store = store
        self.secrets = secrets
        self._finder = FinderService(store, secrets)

    # ----- build -----
    def build(
        self,
        ctx: TenantContext,
        *,
        connection_id: str,
        schema: str | None = None,
        tables: list[str] | None = None,
        watermark_column: str | None = None,
        incremental: bool = False,
        max_rows_per_table: int = MAX_ROWS_PER_TABLE,
        max_text_len: int = MAX_TEXT_LEN,
        config_id: str | None = None,
    ) -> dict:
        """(Re)build the index for a connection. Returns a summary + records a scan run."""
        ctx.require(Permission.READ)
        connector, coords, _ = self._finder._connector(ctx, connection_id)
        dialect = getattr(connector, "dialect", "") or ""
        db = coords.get("database") or ""
        schemas = self._finder._resolve_schemas(ctx, connection_id, coords, schema, None, [], "")

        run_id = self._start_run(ctx, connection_id, config_id, incremental)
        warnings: list[dict] = []
        skipped_detail: list[dict] = []
        tables_indexed = columns_indexed = rows_scanned = date_skipped = skipped_cols = 0
        try:
            for sch in schemas:
                tnames = self._finder._list_tables(ctx, connection_id, sch, None, warnings)
                if tables:
                    wanted = {t.upper() for t in tables}
                    tnames = [t for t in tnames if t.upper() in wanted]
                for tname in tnames:
                    try:
                        cols, rows, dskip, oskip, detail = self._index_table(
                            ctx, connector, dialect, db, sch, tname, connection_id,
                            watermark_column, incremental, max_rows_per_table, warnings, max_text_len,
                        )
                    except Exception as exc:  # noqa: BLE001 — per-table continue
                        warnings.append({"code": "index_failed", "message": f"{sch}.{tname}: {exc}", "details": None})
                        continue
                    tables_indexed += 1
                    columns_indexed += cols
                    rows_scanned += rows
                    date_skipped += dskip
                    skipped_cols += oskip
                    if len(skipped_detail) < 200:  # bounded sample of skipped columns + reasons
                        skipped_detail.extend(detail[: 200 - len(skipped_detail)])
            self._finish_run(ctx, run_id, "ok", tables_indexed, columns_indexed, rows_scanned)
        except Exception as exc:  # noqa: BLE001 — record failure, re-raise for the caller
            self._finish_run(ctx, run_id, "error", tables_indexed, columns_indexed, rows_scanned, str(exc))
            raise
        if date_skipped:
            warnings.append({
                "code": "dates_unsupported",
                "message": f"{date_skipped} date/time column(s) were skipped — date values aren't searchable.",
                "details": None,
            })
        if skipped_cols:
            warnings.append({
                "code": "columns_skipped",
                "message": f"{skipped_cols} column(s) skipped — binary/LOB (CLOB, BLOB, …) or large "
                           f"text (avg length over {max_text_len} chars).",
                "details": None,
            })
        return {
            "run_id": run_id,
            "connection_id": connection_id,
            "scan_type": "incremental" if incremental else "full",
            "tables_indexed": tables_indexed,
            "columns_indexed": columns_indexed,
            "rows_scanned": rows_scanned,
            "date_columns_skipped": date_skipped,
            "columns_skipped": skipped_cols,
            "skipped_columns": skipped_detail,  # [{table, column, reason}] — why each was ignored
            "warnings": warnings,
        }

    def _index_table(
        self, ctx, connector, dialect, db, schema, tname, connection_id,
        watermark_column, incremental, max_rows, warnings, max_text_len,
    ) -> tuple[int, int, int, int, list]:
        ref = TableRef(database=db, schema=schema, table=tname)
        coldefs = connector.get_schema(ref)
        colnames = [c.name for c in coldefs]
        if not colnames:
            return 0, 0, 0, 0, []
        types = {c.name: c.data_type for c in coldefs}
        ref_sql = connector.sql_ref(ref) if hasattr(connector, "sql_ref") else ref.fqn()

        existing = self._load_table_profiles(ctx, connection_id, schema, tname)
        wm_prev = next((p.get("watermark_value") for p in existing.values() if p.get("watermark_value")), None)
        inc = bool(incremental and watermark_column and existing and wm_prev is not None)
        where = f"{_qcol(dialect, watermark_column)} > {_lit(wm_prev)}" if inc else None

        # Date/time and binary/LOB types can't even be aggregated (LENGTH on a BLOB errors), so
        # filter them out up front; everything else is profiled cheaply and then classified.
        date_skipped: set[str] = {cn for cn in colnames if is_date_type(types.get(cn))}
        binary_skipped: set[str] = {
            cn for cn in colnames if cn not in date_skipped and is_binary_type(types.get(cn))
        }
        candidate_cols = [cn for cn in colnames if cn not in date_skipped and cn not in binary_skipped]
        where_clause = f" WHERE {where}" if where else ""
        policy = IndexPolicy(long_len=max_text_len)

        # --- PUSHDOWN #1: one aggregate query → row count (delta on incremental) and, per column,
        # non-null count + avg length + MAX length + approx distinct count — all computed IN the
        # warehouse, no rows pulled. These feed classify_column() so we decide what to index BEFORE
        # the expensive distinct scan. ---
        scanned_rows = 0
        nn: dict[str, int] = {cn: 0 for cn in candidate_cols}
        avglen: dict[str, float] = {cn: 0.0 for cn in candidate_cols}
        maxlen: dict[str, int] = {cn: 0 for cn in candidate_cols}
        ndv: dict[str, float] = {cn: 0.0 for cn in candidate_cols}
        if candidate_cols:
            try:
                agg_sel = ["COUNT(*)"]
                for cn in candidate_cols:
                    q, tq = _qcol(dialect, cn), _text(dialect, _qcol(dialect, cn))
                    agg_sel += [
                        f"COUNT({q})", f"AVG(LENGTH({tq}))", f"MAX(LENGTH({tq}))",
                        count_distinct_sql(dialect, q, approx=True),
                    ]
                agg = connector.execute(f"SELECT {', '.join(agg_sel)} FROM {ref_sql}{where_clause}")  # noqa: S608
                arow = list(agg.rows[0]) if agg.rows else []
                if arow:
                    scanned_rows = int(arow[0] or 0)
                    for j, cn in enumerate(candidate_cols):
                        base = 1 + 4 * j
                        nn[cn] = int(arow[base] or 0)
                        avglen[cn] = float(arow[base + 1] or 0.0)
                        maxlen[cn] = int(arow[base + 2] or 0)
                        ndv[cn] = float(arow[base + 3] or 0.0)
            except Exception:  # noqa: BLE001 — counts are best-effort; the distinct scan still indexes
                pass

        # Classify each candidate (the policy decides index vs ignore, with a reason). date/binary
        # were filtered above; record their reasons too for transparency.
        skip_reason: dict[str, str] = {cn: "binary/LOB type" for cn in binary_skipped}
        scan_cols: list[str] = []
        for cn in candidate_cols:
            ok, reason = classify_column(
                data_type=types.get(cn), non_null=nn[cn], distinct_est=ndv[cn],
                avg_len=avglen[cn], max_len=maxlen[cn], policy=policy,
            )
            if ok:
                scan_cols.append(cn)
            else:
                skip_reason[cn] = reason
        other_skipped = set(skip_reason)  # binary + policy skips (dates reported separately)

        # New high-water mark (best-effort) for the next incremental run — MAX() in-engine.
        wm_new = wm_prev
        if watermark_column and watermark_column in colnames:
            try:
                r = connector.execute(
                    f"SELECT {_text(dialect, f'MAX({_qcol(dialect, watermark_column)})')} AS wm "  # noqa: S608
                    f"FROM {ref_sql}"
                )
                top = list(r.rows)
                if top and top[0][0] is not None:
                    wm_new = top[0][0] if isinstance(top[0][0], str) else str(top[0][0])
            except Exception:  # noqa: BLE001 — watermark is best-effort
                pass

        # --- PUSHDOWN #2: pull only DISTINCT values (dedup happens in the warehouse), capped, for
        # MANY columns per round-trip via a chunked UNION ALL. Transfers the distinct cardinality,
        # NOT the row count — a value repeated a billion times comes back once. Set-invariant, so
        # this is exact. One query covers up to CHUNK columns instead of one query per column. ---
        distinct_by_col = self._fetch_distinct_multi(
            connector, dialect, ref_sql, where, scan_cols, max_rows, warnings, tname
        )

        now = utcnow()
        persisted = 0
        with self.store.session(ctx) as s:
            def drop_stale(column: str) -> None:
                prev = existing.get(column)
                if prev:
                    obj = s.get(FinderColumnProfile, prev["id"])
                    if obj is not None:
                        s.delete(obj)  # remove a profile that should no longer be indexed

            # Purge stale profiles for columns we now skip (date, binary/LOB, or large text).
            for cn in date_skipped | other_skipped:
                drop_stale(cn)

            for cn in scan_cols:
                prev = existing.get(cn)
                distinct_vals = distinct_by_col.get(cn, [])
                stats = ColumnStats.from_prior(prev if inc else None)
                bf = _seed_bloom(prev, inc, len(distinct_vals))
                for sval in distinct_vals:
                    stats.add(sval)
                    bf.add(sval)
                meta = stats.finalize()

                null_delta = max(0, scanned_rows - nn[cn])
                rc = scanned_rows if not inc else ((prev or {}).get("row_count") or 0) + scanned_rows
                nc = null_delta if not inc else ((prev or {}).get("null_count") or 0) + null_delta
                fields = {
                    "source_data_type": types.get(cn), "row_count": rc, "null_count": nc,
                    "inferred_type": meta["inferred_type"], "is_numeric": meta["is_numeric"],
                    "is_date": meta["is_date"], "num_min": meta["num_min"], "num_max": meta["num_max"],
                    "min_len": meta["min_len"], "max_len": meta["max_len"],
                    "charset_mask": meta["charset_mask"], "pattern_masks": meta["pattern_masks"],
                    "pattern_exhaustive": meta["pattern_exhaustive"], "distinct_est": meta["distinct_est"],
                    "is_low_card": meta["is_low_card"], "distinct_values": meta["distinct_values"],
                    "watermark_value": wm_new, "last_scanned_at": now,
                }
                if meta["is_low_card"]:  # exact set beats a Bloom — store it, drop the filter
                    fields.update(bloom=None, bloom_m=0, bloom_k=0, bloom_bits_set=None)
                else:
                    fields.update(bloom=bf.to_bytes(), bloom_m=bf.m, bloom_k=bf.k, bloom_bits_set=bf.bits_set())
                obj = s.get(FinderColumnProfile, prev["id"]) if prev else None
                if obj is not None:
                    for k, val in fields.items():
                        setattr(obj, k, val)
                else:
                    s.add(FinderColumnProfile(
                        org_id=ctx.org_id, group_id=ctx.group_id, created_by=ctx.user_id,
                        connection_id=connection_id, schema_name=schema, table_name=tname,
                        column_name=cn, **fields,
                    ))
                persisted += 1
        detail = [{"table": tname, "column": cn, "reason": r} for cn, r in sorted(skip_reason.items())]
        return persisted, scanned_rows, len(date_skipped), len(other_skipped), detail

    def _fetch_distinct_multi(self, connector, dialect, ref_sql, where, cols, cap, warnings, tname):
        """DISTINCT values for MANY columns in FEW round-trips: a chunked UNION ALL where each
        branch de-dups + caps one column IN the database. One query covers up to CHUNK_COLUMNS
        columns instead of one query per column. Falls back to per-column on a batch error so one
        bad column (e.g. a LOB that won't cast to text) never loses the rest. → {column: [values]}."""
        out: dict[str, list[str]] = {cn: [] for cn in cols}
        indexed = list(enumerate(cols))  # stable integer tag per column, mapped back on read
        by_tag = {i: cn for i, cn in indexed}

        def _one(cn: str) -> str:  # a single column's capped DISTINCT select, as a subquery
            colexpr = _text(dialect, _qcol(dialect, cn))
            w = f"{_qcol(dialect, cn)} IS NOT NULL" + (f" AND {where}" if where else "")
            return _limited(dialect, f"SELECT DISTINCT {colexpr} AS v FROM {ref_sql} WHERE {w}", cap)  # noqa: S608

        def _branch(i: int, cn: str) -> str:  # tagged with the column's integer index for UNION ALL
            colexpr = _text(dialect, _qcol(dialect, cn))
            w = f"{_qcol(dialect, cn)} IS NOT NULL" + (f" AND {where}" if where else "")
            inner = f"SELECT DISTINCT {i} AS ci, {colexpr} AS v FROM {ref_sql} WHERE {w}"  # noqa: S608
            return f"SELECT ci, v FROM ({_limited(dialect, inner, cap)})"

        for start in range(0, len(indexed), CHUNK_COLUMNS):
            batch = indexed[start:start + CHUNK_COLUMNS]
            try:
                sql = " UNION ALL ".join(_branch(i, cn) for i, cn in batch)
                for row in connector.execute(sql).rows:
                    v = row[1]
                    if v is not None:
                        out[by_tag[int(row[0])]].append(v if isinstance(v, str) else str(v))
            except Exception:  # noqa: BLE001 — a bad column shouldn't sink the batch; retry per column
                for _, cn in batch:
                    try:
                        out[cn] = [
                            (r[0] if isinstance(r[0], str) else str(r[0]))
                            for r in connector.execute(_one(cn)).rows if r[0] is not None
                        ]
                    except Exception as exc:  # noqa: BLE001 — surface + skip just this column
                        warnings.append({"code": "column_scan_failed",
                                         "message": f"{tname}.{cn}: {exc}", "details": None})
        return out

    # ----- scheduled refresh -----
    def refresh_due(self, ctx: TenantContext, now=None) -> int:
        """Run incremental refresh for every enabled config whose next_run_at has passed.
        Called by the scheduler; capped to at most daily by how next_run_at is advanced."""
        from datetime import timedelta

        now = now or utcnow()
        with self.store.session(ctx) as s:
            from sqlalchemy import select

            configs = s.scalars(
                select(FinderScanConfig).where(
                    FinderScanConfig.group_id == ctx.group_id,
                    FinderScanConfig.enabled.is_(True),
                    FinderScanConfig.frequency == "daily",
                )
            ).all()
            due = [
                (c.id, c.created_by, c.connection_id, c.schema_name, c.tables, c.watermark_column)
                for c in configs
                if c.next_run_at is None or _aware(c.next_run_at) <= now
            ]
        ran = 0
        for cid, owner, conn_id, sch, tbls, wm in due:
            # Rebuild under the config OWNER's identity so the refreshed index stays theirs.
            owner_ctx = ctx
            if owner and owner != ctx.user_id:
                from dvp.services.tenancy import resolve_context

                owner_ctx = resolve_context(
                    self.store, user_id=owner, org_id=ctx.org_id, group_id=ctx.group_id
                )
            try:
                self.build(
                    owner_ctx, connection_id=conn_id, schema=sch, tables=tbls,
                    watermark_column=wm, incremental=bool(wm), config_id=cid,
                )
                ran += 1
            except Exception:  # noqa: BLE001 — one bad config shouldn't stop the rest
                pass
            with self.store.session(ctx) as s:
                cfg = s.get(FinderScanConfig, cid)
                if cfg is not None:
                    cfg.last_run_at = now
                    cfg.next_run_at = now + timedelta(days=1)
        return ran

    # ----- config + status -----
    def set_config(
        self, ctx: TenantContext, *, connection_id: str, schema: str | None = None,
        tables: list[str] | None = None, watermark_column: str | None = None,
        frequency: str = "daily", enabled: bool = True,
    ) -> dict:
        ctx.require(Permission.READ)
        from sqlalchemy import select

        with self.store.session(ctx) as s:
            cfg = s.scalars(
                select(FinderScanConfig).where(
                    FinderScanConfig.group_id == ctx.group_id,
                    FinderScanConfig.created_by == ctx.user_id,
                    FinderScanConfig.connection_id == connection_id,
                )
            ).first()
            if cfg is None:
                cfg = FinderScanConfig(
                    org_id=ctx.org_id, group_id=ctx.group_id, created_by=ctx.user_id,
                    connection_id=connection_id,
                )
                s.add(cfg)
            cfg.schema_name = schema
            cfg.tables = tables
            cfg.watermark_column = watermark_column
            cfg.frequency = frequency
            cfg.enabled = enabled
            if cfg.next_run_at is None:
                cfg.next_run_at = utcnow()
            s.flush()
            return _config_dict(cfg)

    def status(self, ctx: TenantContext, connection_id: str) -> dict:
        """Index coverage + latest run + config for one connection."""
        ctx.require(Permission.READ)
        from sqlalchemy import func, select

        with self.store.session(ctx) as s:
            prof = s.execute(
                select(
                    func.count(FinderColumnProfile.id),
                    func.count(func.distinct(FinderColumnProfile.table_name)),
                    func.max(FinderColumnProfile.last_scanned_at),
                ).where(
                    FinderColumnProfile.group_id == ctx.group_id,
                    FinderColumnProfile.created_by == ctx.user_id,
                    FinderColumnProfile.connection_id == connection_id,
                )
            ).one()
            last_run = s.scalars(
                select(FinderScanRun).where(
                    FinderScanRun.group_id == ctx.group_id,
                    FinderScanRun.created_by == ctx.user_id,
                    FinderScanRun.connection_id == connection_id,
                ).order_by(FinderScanRun.created_at.desc())
            ).first()
            cfg = s.scalars(
                select(FinderScanConfig).where(
                    FinderScanConfig.group_id == ctx.group_id,
                    FinderScanConfig.created_by == ctx.user_id,
                    FinderScanConfig.connection_id == connection_id,
                )
            ).first()
            return {
                "connection_id": connection_id,
                "indexed": bool(prof[0]),
                "columns_indexed": int(prof[0] or 0),
                "tables_indexed": int(prof[1] or 0),
                "last_scanned_at": prof[2].isoformat() if prof[2] else None,
                "last_run": _run_dict(last_run) if last_run else None,
                "config": _config_dict(cfg) if cfg else None,
            }

    # ----- internals -----
    def _load_table_profiles(self, ctx, connection_id, schema, table) -> dict:
        from sqlalchemy import select

        with self.store.session(ctx) as s:
            rows = s.scalars(
                select(FinderColumnProfile).where(
                    FinderColumnProfile.group_id == ctx.group_id,
                    FinderColumnProfile.created_by == ctx.user_id,  # per-user index
                    FinderColumnProfile.connection_id == connection_id,
                    FinderColumnProfile.schema_name == schema,
                    FinderColumnProfile.table_name == table,
                )
            ).all()
            # Snapshot to plain dicts so ColumnStats.from_prior can seed outside the session.
            return {
                r.column_name: {
                    "id": r.id, "row_count": r.row_count, "null_count": r.null_count,
                    "is_numeric": r.is_numeric, "inferred_type": r.inferred_type,
                    "num_min": r.num_min, "num_max": r.num_max,
                    "min_len": r.min_len, "max_len": r.max_len, "charset_mask": r.charset_mask,
                    "pattern_masks": r.pattern_masks, "pattern_exhaustive": r.pattern_exhaustive,
                    "is_low_card": r.is_low_card, "distinct_values": r.distinct_values,
                    "bloom": r.bloom, "bloom_m": r.bloom_m, "bloom_k": r.bloom_k,
                    "watermark_value": r.watermark_value,
                }
                for r in rows
            }

    def _start_run(self, ctx, connection_id, config_id, incremental) -> str:
        with self.store.session(ctx) as s:
            run = FinderScanRun(
                org_id=ctx.org_id, group_id=ctx.group_id, created_by=ctx.user_id,
                connection_id=connection_id, config_id=config_id,
                scan_type="incremental" if incremental else "full",
                status="running", started_at=utcnow(),
            )
            s.add(run)
            s.flush()
            return run.id

    def _finish_run(self, ctx, run_id, status, tables, columns, rows, error=None) -> None:
        with self.store.session(ctx) as s:
            run = s.get(FinderScanRun, run_id)
            if run is not None:
                run.status = status
                run.tables_indexed = tables
                run.columns_indexed = columns
                run.rows_scanned = rows
                run.error = error
                run.finished_at = utcnow()


def _seed_bloom(prev, inc, distinct_count):  # noqa: ANN001, ANN202
    """A Bloom sized for the column's DISTINCT cardinality (now known exactly, since we pulled the
    distinct set), seeded from the stored profile on an incremental run so the merge stays complete."""
    if inc and prev and prev.get("bloom") and prev.get("bloom_m"):
        return BloomFilter(prev["bloom_m"], prev["bloom_k"], prev["bloom"])
    if inc and prev and prev.get("is_low_card") and prev.get("distinct_values"):
        bf = BloomFilter.sized(max(distinct_count, len(prev["distinct_values"])) or 1000, DEFAULT_FP)
        for dv in prev["distinct_values"]:
            bf.add(dv)
        return bf
    return BloomFilter.sized(distinct_count or 1000, DEFAULT_FP)


def _aware(dt):  # noqa: ANN001, ANN201 — normalize naive DB datetimes to UTC-aware
    from datetime import UTC

    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _config_dict(cfg) -> dict:  # noqa: ANN001
    return {
        "connection_id": cfg.connection_id,
        "schema": cfg.schema_name,
        "tables": cfg.tables,
        "watermark_column": cfg.watermark_column,
        "frequency": cfg.frequency,
        "enabled": cfg.enabled,
        "last_run_at": cfg.last_run_at.isoformat() if cfg.last_run_at else None,
        "next_run_at": cfg.next_run_at.isoformat() if cfg.next_run_at else None,
    }


def _run_dict(run) -> dict:  # noqa: ANN001
    return {
        "id": run.id,
        "scan_type": run.scan_type,
        "status": run.status,
        "tables_indexed": run.tables_indexed,
        "columns_indexed": run.columns_indexed,
        "rows_scanned": run.rows_scanned,
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }
