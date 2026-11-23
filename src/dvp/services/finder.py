"""FinderService — "Find Column": given a value (or a few values), discover which
tables & columns in a connection contain it, and return confirmed sample rows.

This is the live-scan MVP of the Value Locator product (see PRD_Value_Locator.md). It
implements the product's two-phase idea directly against the source — there is no
pre-built Bloom index yet:

  * Phase 1 (shortlist): for every column of every in-scope table, run ONE aggregate
    query per table that counts, per (value, column) pair, how many rows match. That
    tells us which columns hold each value.
  * Phase 2 (confirm & sample): for a hit, run an exact `SELECT * … WHERE … LIMIT n`
    so the user sees real rows. For multi-value "co-occurrence" mode the predicate is
    conjunctive (every value must appear, in the SAME row) — e.g. zip `27519` AND
    state `NC` — which a per-column index alone can never prove.

Matching is EXACT (per the product decision) but value-vs-column comparison is done by
casting the column to text and comparing as a string, so a numeric column and a string
value never raise a type error and `27519` matches whether stored as text or number.
Per-table failures are surfaced as warnings and skipped (never a 500), mirroring the
rest of the catalog surface.
"""

from __future__ import annotations

import re
from time import perf_counter

from dvp.abstractions.secret_store import SecretStore
from dvp.connectors.base import TableRef
from dvp.core.errors import NotFoundError, ValidationError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.models import Connection, FinderColumnProfile, FinderSearch
from dvp.db.session import MetadataStore
from dvp.services.bloom import BloomFilter

MAX_VALUES = 5
DEFAULT_MAX_TABLES = 200


class FinderService:
    def __init__(self, store: MetadataStore, secrets: SecretStore) -> None:
        self.store = store
        self.secrets = secrets

    def search(
        self,
        ctx: TenantContext,
        *,
        connection_id: str | None = None,
        connection_ids: list[str] | None = None,
        values: list[str],
        schema: str | None = None,
        schemas: list[str] | None = None,
        database: str | None = None,
        tables: list[str] | None = None,
        match_mode: str = "any",
        sample_limit: int = 20,
        max_tables: int = DEFAULT_MAX_TABLES,
        use_index: str = "auto",
        confirm_concurrency: int = 4,
    ) -> dict:
        """Search for `values` across one or more connections/schemas.

        Scope resolution:
          * connections — `connection_ids` (or single `connection_id`) if given, else EVERY
            connection the caller may read.
          * schemas — `schemas` (or single `schema`) if given, else every schema the connector
            exposes (falling back to the connection's default).

        match_mode:
          * "any"           — a table is a hit if it contains ANY of the values (each value
                              reported with the column(s) it was found in).
          * "co_occurrence" — a table is a hit only if EVERY value appears in the SAME row
                              (conjunctive). Requires >= 2 values to differ from "any".

        `max_tables` is a GLOBAL budget across the whole scan so a broad all-connections search
        can't run away; when it's exhausted the result is flagged `truncated`.
        """
        ctx.require(Permission.READ)
        vals = [str(v).strip() for v in (values or []) if str(v).strip() != ""]
        if not vals:
            raise ValidationError("provide at least one value to search for")
        if len(vals) > MAX_VALUES:
            raise ValidationError(f"at most {MAX_VALUES} values per search", details={"got": len(vals)})
        n = max(1, min(int(sample_limit or 20), 100))
        cap = max(1, min(int(max_tables or DEFAULT_MAX_TABLES), 1000))
        workers = max(1, min(int(confirm_concurrency or 4), 32))
        co = match_mode == "co_occurrence" and len(vals) >= 2

        conns = self._resolve_connections(ctx, connection_id, connection_ids)
        warnings: list[dict] = []
        hits: list[dict] = []
        scanned = 0
        scanned_conns = 0
        remaining = cap
        truncated = False
        used_index = False
        t_start = perf_counter()
        stats = {"took_ms": 0.0, "shortlist_ms": 0.0, "confirm_ms": 0.0, "blooms_loaded": 0,
                 "candidate_tables": 0, "tables_confirmed": 0, "scanned_columns": 0}

        for cid, cname in conns:
            if remaining <= 0:
                truncated = True
                break
            try:
                make, coords, _ = self._connector_factory(ctx, cid)
                connector = make()
            except Exception as exc:  # noqa: BLE001 — per-connection continue
                warnings.append(_warn("connection_failed", f"{cname}: {exc}"))
                continue
            scanned_conns += 1
            dialect = getattr(connector, "dialect", "") or ""
            db = database or coords.get("database") or ""

            # Scope the schemas. For an indexed connection we derive the candidate (table, column)
            # set straight from the index via the metadata cascade — no table listing, no Bloom
            # loaded until a column survives every cheap gate.
            schema_scope = (
                set(schemas) if schemas else ({schema} if schema else None)
            )
            if use_index != "never" and self._has_index(ctx, cid):
                t0 = perf_counter()
                candidates, sel_by_key, col_types, blooms_loaded = self._shortlist(
                    ctx, cid, schema_scope, vals, co
                )
                stats["shortlist_ms"] += (perf_counter() - t0) * 1000
                stats["blooms_loaded"] += blooms_loaded
                stats["candidate_tables"] += len(candidates)
                used_index = used_index or True

                # Candidate tables to confirm, within the table filter + global budget.
                items = [
                    (sch, tname, per_value)
                    for (sch, tname), per_value in candidates.items()
                    if not (tables and tname.upper() not in {t.upper() for t in tables})
                ]
                if len(items) > remaining:
                    items = items[:remaining]
                    truncated = True
                remaining -= len(items)

                tc = perf_counter()
                results = self._confirm_tables(
                    make, connector, dialect, db, cid, cname, vals, co, n, items,
                    col_types, warnings, workers, coords,
                )
                stats["confirm_ms"] += (perf_counter() - tc) * 1000
                for (sch, tname), (hit, did_scan, _, cols) in results:
                    if did_scan:
                        scanned += 1
                        stats["tables_confirmed"] += 1
                        stats["scanned_columns"] += cols
                    if hit is not None:
                        hit["selectivity"] = sel_by_key.get((sch, tname), 0.0)
                        hits.append(hit)
                continue

            # No index → live scan of every table (cached listing).
            for sch in self._resolve_schemas(ctx, cid, coords, schema, schemas, warnings, cname):
                if remaining <= 0:
                    truncated = True
                    break
                table_names = self._list_tables(ctx, cid, sch, database, warnings)
                if tables:
                    wanted = {t.upper() for t in tables}
                    table_names = [t for t in table_names if t.upper() in wanted]
                if len(table_names) > remaining:
                    table_names = table_names[:remaining]
                    truncated = True
                for tname in table_names:
                    remaining -= 1
                    tc = perf_counter()
                    hit, did_scan, _, cols = self._scan_table(
                        connector, dialect, db, sch, tname, vals, co=co, limit=n,
                        connection_id=cid, connection_name=cname, warnings=warnings,
                    )
                    stats["confirm_ms"] += (perf_counter() - tc) * 1000
                    if did_scan:
                        scanned += 1
                        stats["tables_confirmed"] += 1
                        stats["scanned_columns"] += cols
                    if hit is not None:
                        hits.append(hit)

        # Rank: most-selective column first (a hit in a near-unique ID column beats one in a
        # low-cardinality flag), then by match volume. Live-scan hits (no selectivity) sort last.
        hits.sort(key=lambda h: (h.get("selectivity") or 0.0, h.get("match_count") or 0), reverse=True)
        if truncated:
            warnings.append(_warn(
                "tables_truncated",
                f"hit the {cap}-table scan budget; narrow the scope (pick a connection/schema) to cover the rest",
            ))
        # Nudge toward a narrower scope — a broad scan is far slower, especially without an index.
        if not (schema or schemas):
            warnings.append(_warn(
                "broad_scope",
                "No schema specified, so every schema was scanned — this is much slower. "
                "Provide a schema name for a fast search; use 'all schemas' only as a last resort.",
            ))
        if not (connection_id or connection_ids) and scanned_conns > 1:
            warnings.append(_warn(
                "broad_scope",
                f"No connection specified, so all {scanned_conns} connections were scanned. "
                "Pick a connection to search faster.",
            ))
        # Dates aren't supported — date/time columns aren't indexed, so a date value won't match.
        from dvp.services.column_profile import is_date as _looks_date

        if any(_looks_date(v) for v in vals):
            warnings.append(_warn(
                "dates_unsupported",
                "Date values aren't supported — Find Column does not index date/time columns, "
                "so a date won't be found even if it exists.",
            ))
        stats["took_ms"] = round((perf_counter() - t_start) * 1000, 1)
        stats["shortlist_ms"] = round(stats["shortlist_ms"], 1)
        stats["confirm_ms"] = round(stats["confirm_ms"], 1)
        result = {
            "match_mode": "co_occurrence" if co else "any",
            "values": vals,
            "scanned_connections": scanned_conns,
            "scanned_tables": scanned,
            "matched_tables": len(hits),
            "truncated": truncated,
            "used_index": used_index,
            "stats": stats,
            "hits": hits,
            "warnings": warnings,
        }
        # Persist to the user's search history (best-effort — history must never fail a search).
        result["search_id"] = self._persist_search(
            ctx, result=result, conns=conns,
            connection_id=connection_id, connection_ids=connection_ids,
            schema=schema, schemas=schemas,
        )
        return result

    # ----- search history (per-user) -----
    def list_searches(self, ctx: TenantContext, *, limit: int = 50) -> list[dict]:
        """Most-recent-first summaries of THIS user's searches (no heavy result payload)."""
        from sqlalchemy import desc, select

        with self.store.session(ctx) as s:
            rows = s.scalars(
                select(FinderSearch)
                .where(
                    FinderSearch.group_id == ctx.group_id,
                    FinderSearch.created_by == ctx.user_id,
                )
                .order_by(desc(FinderSearch.created_at))
                .limit(limit)
            ).all()
            return [self._search_summary(r) for r in rows]

    def get_search(self, ctx: TenantContext, search_id: str) -> dict:
        """Re-open a past search — summary plus the full stored result (hits + sample rows)."""
        with self.store.session(ctx) as s:
            row = self._owned_search(ctx, s, search_id)
            d = self._search_summary(row)
            d["result"] = row.result
            return d

    def delete_search(self, ctx: TenantContext, search_id: str) -> None:
        with self.store.session(ctx) as s:
            s.delete(self._owned_search(ctx, s, search_id))

    def _owned_search(self, ctx: TenantContext, s, search_id: str) -> FinderSearch:  # noqa: ANN001
        row = s.get(FinderSearch, search_id)
        # Per-user: a search is private to the user who ran it (not just the group).
        if row is None or row.group_id != ctx.group_id or row.created_by != ctx.user_id:
            raise NotFoundError("search not found", details={"id": search_id})
        return row

    @staticmethod
    def _search_summary(row: FinderSearch) -> dict:
        return {
            "id": row.id,
            "values": row.values,
            "match_mode": row.match_mode,
            "scope_label": row.scope_label,
            "scanned_connections": row.scanned_connections,
            "scanned_tables": row.scanned_tables,
            "scanned_columns": row.scanned_columns,
            "matched_tables": row.matched_tables,
            "total_matches": row.total_matches,
            "took_ms": row.took_ms,
            "used_index": row.used_index,
            "truncated": row.truncated,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    def _persist_search(
        self, ctx: TenantContext, *, result: dict, conns: list,
        connection_id: str | None, connection_ids: list[str] | None,
        schema: str | None, schemas: list[str] | None,
    ) -> str | None:
        """Write one history row. Best-effort: a persistence failure logs nothing fatal and the
        search still returns (history is a convenience, not part of the search contract)."""
        try:
            names = [cn for _, cn in conns]
            if connection_id or (connection_ids and len(connection_ids) == 1):
                conn_label = names[0] if names else "1 connection"
            elif connection_ids:
                conn_label = f"{len(connection_ids)} connections"
            else:
                conn_label = "All connections"
            if schema:
                scope = f"{conn_label} · {schema}"
            elif schemas:
                scope = f"{conn_label} · {len(schemas)} schema(s)"
            else:
                scope = f"{conn_label} · all schemas"
            total = sum(h.get("match_count") or 0 for h in result["hits"])
            row = FinderSearch(
                org_id=ctx.org_id, group_id=ctx.group_id, created_by=ctx.user_id,
                values=result["values"], match_mode=result["match_mode"], scope_label=scope,
                connection_ids=connection_ids or ([connection_id] if connection_id else None),
                schemas=schemas or ([schema] if schema else None),
                scanned_connections=result["scanned_connections"],
                scanned_tables=result["scanned_tables"],
                scanned_columns=result["stats"].get("scanned_columns", 0),
                matched_tables=result["matched_tables"], total_matches=total,
                took_ms=result["stats"].get("took_ms", 0.0),
                used_index=result["used_index"], truncated=result["truncated"],
                result=result,
            )
            with self.store.session(ctx) as s:
                s.add(row)
                s.flush()
                return row.id
        except Exception:  # noqa: BLE001 — history is best-effort, never breaks a search
            return None

    # ----- internals -----
    def _scan_table(
        self, connector, dialect: str, db: str, schema: str, tname: str,
        vals: list[str], *, co: bool, limit: int,
        connection_id: str, connection_name: str, warnings: list[dict],
        candidate_cols: list[str] | None = None,
        col_types: dict[str, str | None] | None = None,
    ) -> tuple[dict | None, bool, bool, int]:
        """Confirm + sample one table. `candidate_cols` (from the index cascade) restricts the
        exact-match query to a handful of columns, and `col_types` lets the predicate be sargable
        (native typed compare → source index). Without them (live fallback) we read the full
        column list and use cross-type-safe CAST predicates. Returns
        (hit, did_scan, used_index, columns_scanned)."""
        ref = TableRef(database=db, schema=schema, table=tname)
        used_index = candidate_cols is not None
        if candidate_cols is not None:
            scan_cols = candidate_cols
        else:
            from dvp.services.column_profile import is_date_type

            try:
                # Skip date/time columns — dates aren't supported (consistent with the index).
                scan_cols = [c.name for c in connector.get_schema(ref) if not is_date_type(c.data_type)]
            except Exception as exc:  # noqa: BLE001 — per-table continue
                warnings.append(_warn("schema_failed", f"{connection_name}.{tname}: {exc}"))
                return None, False, False, 0
        ncols = len(scan_cols)
        if not scan_cols:
            return None, True, used_index, 0

        ref_sql = connector.sql_ref(ref) if hasattr(connector, "sql_ref") else ref.fqn()
        try:
            matched, match_count = self._match_columns(
                connector, dialect, ref_sql, scan_cols, vals, co=co, col_types=col_types
            )
        except Exception as exc:  # noqa: BLE001 — per-table continue
            warnings.append(_warn("scan_failed", f"{connection_name}.{tname}: {exc}"))
            return None, False, used_index, ncols

        if not any(matched[i] for i in range(len(vals))):
            return None, True, used_index, ncols  # confirmed absent (index false positive)
        if co and not all(matched[i] for i in range(len(vals))):
            return None, True, used_index, ncols  # co-occurrence needs every value present here
        if match_count == 0:
            return None, True, used_index, ncols  # columns matched separately but no shared row

        hit = self._build_hit(
            connector, dialect, ref_sql, vals, matched, match_count, co=co, limit=limit,
            connection_id=connection_id, connection_name=connection_name,
            database=db, schema=schema, table=tname, warnings=warnings, col_types=col_types,
        )
        return hit, True, used_index, ncols

    def _confirm_tables(
        self, make, connector, dialect: str, db: str, connection_id: str, connection_name: str,
        vals: list[str], co: bool, limit: int, items: list, col_types: dict, warnings: list[dict],
        workers: int, coords: dict,
    ) -> list:
        """Confirm+sample each candidate table. Confirms are independent source queries, so on a
        warehouse (or in-memory local) we run them in a bounded thread pool — latency becomes the
        slowest confirm, not the sum. Each worker gets its OWN connector (connectors aren't
        thread-safe) via `make()`. File-backed SQLite can't be rebuilt per thread, so it stays
        serial; the primary connector is reused there."""

        def one(conn, item):
            sch, tname, per_value = item
            cand_cols = sorted({c for cols in per_value.values() for c in cols})
            table_types = {c: col_types.get((sch, tname, c)) for c in cand_cols}
            return (sch, tname), self._scan_table(
                conn, dialect, db, sch, tname, vals, co=co, limit=limit,
                connection_id=connection_id, connection_name=connection_name, warnings=warnings,
                candidate_cols=cand_cols, col_types=table_types,
            )

        parallel = workers > 1 and len(items) > 1 and not coords.get("sqlite_path")
        if not parallel:
            return [one(connector, it) for it in items]

        import threading
        from concurrent.futures import ThreadPoolExecutor

        tl = threading.local()

        def run(item):
            conn = getattr(tl, "conn", None)
            if conn is None:
                try:
                    conn = tl.conn = make()
                except Exception as exc:  # noqa: BLE001 — a dead worker shouldn't sink the search
                    warnings.append(_warn("connection_failed", f"{connection_name}: {exc}"))
                    return (item[0], item[1]), (None, False, True, 0)
            return one(conn, item)

        with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
            return list(ex.map(run, items))

    def _has_index(self, ctx: TenantContext, connection_id: str) -> bool:
        from sqlalchemy import select

        with self.store.session(ctx) as s:
            return s.scalars(
                select(FinderColumnProfile.id).where(
                    FinderColumnProfile.group_id == ctx.group_id,
                    FinderColumnProfile.created_by == ctx.user_id,  # per-user: only this user's index
                    FinderColumnProfile.connection_id == connection_id,
                ).limit(1)
            ).first() is not None

    def _shortlist(
        self, ctx: TenantContext, connection_id: str, schema_scope: set[str] | None,
        vals: list[str], co: bool,
    ) -> tuple[
        dict[tuple[str, str], dict[int, set[str]]],
        dict[tuple[str, str], float],
        dict[tuple[str, str, str], str | None],
        int,
    ]:
        """The pruning cascade. The cheap gates (length, charset subset, type) are PUSHED INTO SQL
        so the DB returns only candidate columns — no O(all-columns) scan in Python and no Bloom
        blob loaded until a column survives. Numeric-range + format-mask refine the few survivors
        in app; low-card columns confirm via their exact set, high-card via the Bloom.

        Returns (survivors {(schema,table): {value_idx: {cols}}}, selectivity, col-type tags, and
        the number of Bloom blobs loaded — for telemetry)."""
        from decimal import Decimal

        from sqlalchemy import and_, or_, select

        from dvp.services.column_profile import value_features

        vfs = [value_features(v) for v in vals]

        survivors: dict[tuple[str, str], dict[int, set[str]]] = {}
        sel_by_key: dict[tuple[str, str], float] = {}
        col_types: dict[tuple[str, str, str], str | None] = {}
        deferred: list[tuple[tuple[str, str], int, str, str]] = []  # (key, value_idx, column, id)

        def bump_sel(key, value):  # noqa: ANN001, ANN202
            if value > sel_by_key.get(key, 0.0):
                sel_by_key[key] = value

        # 1) Per value, let the DB apply gates ①②④ and return only the candidate columns.
        with self.store.session(ctx) as s:
            for i, vf in enumerate(vfs):
                q = select(
                    FinderColumnProfile.id, FinderColumnProfile.schema_name,
                    FinderColumnProfile.table_name, FinderColumnProfile.column_name,
                    FinderColumnProfile.is_numeric, FinderColumnProfile.inferred_type,
                    FinderColumnProfile.num_min, FinderColumnProfile.num_max,
                    FinderColumnProfile.pattern_masks, FinderColumnProfile.pattern_exhaustive,
                    FinderColumnProfile.is_low_card, FinderColumnProfile.distinct_values,
                    FinderColumnProfile.distinct_est, FinderColumnProfile.row_count,
                ).where(
                    FinderColumnProfile.group_id == ctx.group_id,
                    FinderColumnProfile.created_by == ctx.user_id,  # per-user index
                    FinderColumnProfile.connection_id == connection_id,
                    # ② length (null min_len = no data → keep; harmless, confirm filters)
                    or_(
                        FinderColumnProfile.min_len.is_(None),
                        and_(
                            FinderColumnProfile.min_len <= vf["len"],
                            FinderColumnProfile.max_len >= vf["len"],
                        ),
                    ),
                    # ④ charset subset: every class in the value was seen in the column
                    FinderColumnProfile.charset_mask.op("&")(vf["charset"]) == vf["charset"],
                )
                if schema_scope:
                    q = q.where(FinderColumnProfile.schema_name.in_(schema_scope))
                if vf["num"] is None:  # ① non-numeric value can't live in a numeric-only column
                    q = q.where(FinderColumnProfile.is_numeric.is_(False))

                for row in s.execute(q).all():
                    key = (row.schema_name, row.table_name)
                    col_types[(row.schema_name, row.table_name, row.column_name)] = col_tag(
                        row.is_numeric, row.inferred_type
                    )
                    # ③ numeric range (exact Decimal) — refine the already-narrow set in app
                    if row.is_numeric and vf["num"] is not None:
                        if row.num_min is not None and vf["num"] < Decimal(row.num_min):
                            continue
                        if row.num_max is not None and vf["num"] > Decimal(row.num_max):
                            continue
                    # ⑤ format mask (only when the column captured all its masks)
                    if row.pattern_exhaustive and row.pattern_masks is not None and vf["pattern"] is not None:
                        if vf["pattern"] not in set(row.pattern_masks):
                            continue
                    sel = ((row.distinct_est or 0) / row.row_count) if (row.is_low_card and row.row_count) else 1.0
                    if row.is_low_card:
                        if row.distinct_values and vf["text"] in set(row.distinct_values):  # ⑥ exact
                            survivors.setdefault(key, {}).setdefault(i, set()).add(row.column_name)
                            bump_sel(key, sel)
                    else:
                        deferred.append((key, i, row.column_name, row.id))  # needs a Bloom test

        # 2) Load Bloom blobs only for the high-cardinality survivors, then test membership ⑥.
        blooms_loaded = 0
        if deferred:
            blooms = self._load_blooms(ctx, {d[3] for d in deferred})
            blooms_loaded = len(blooms)
            for key, i, column, pid in deferred:
                bf = blooms.get(pid)
                if bf is not None and vals[i] in bf:
                    survivors.setdefault(key, {}).setdefault(i, set()).add(column)
                    bump_sel(key, 1.0)

        # 3) Co-occurrence prune: every value must have at least one candidate column in the table.
        if co:
            nvals = len(vals)
            survivors = {k: pv for k, pv in survivors.items() if all(pv.get(i) for i in range(nvals))}
            sel_by_key = {k: sel_by_key.get(k, 0.0) for k in survivors}
        return survivors, sel_by_key, col_types, blooms_loaded

    def _load_blooms(self, ctx: TenantContext, ids: set[str]) -> dict[str, BloomFilter]:
        from sqlalchemy import select

        out: dict[str, BloomFilter] = {}
        with self.store.session(ctx) as s:
            rows = s.execute(
                select(
                    FinderColumnProfile.id, FinderColumnProfile.bloom,
                    FinderColumnProfile.bloom_m, FinderColumnProfile.bloom_k,
                ).where(
                    FinderColumnProfile.group_id == ctx.group_id,
                    FinderColumnProfile.created_by == ctx.user_id,  # per-user index
                    FinderColumnProfile.id.in_(ids),
                )
            ).all()
            for r in rows:
                if r.bloom and r.bloom_m:
                    out[r.id] = BloomFilter(r.bloom_m, r.bloom_k, r.bloom)
        return out

    def _resolve_connections(
        self, ctx: TenantContext, connection_id: str | None, connection_ids: list[str] | None
    ) -> list[tuple[str, str]]:
        """(id, name) pairs to scan: the explicit ones (validated), else every readable connection."""
        ids = list(connection_ids) if connection_ids else ([connection_id] if connection_id else [])
        if ids:
            out: list[tuple[str, str]] = []
            with self.store.session(ctx) as s:
                for cid in ids:
                    conn = s.get(Connection, cid)
                    if conn is None or conn.group_id != ctx.group_id:
                        raise ValidationError("connection not found", details={"id": cid})
                    out.append((conn.id, conn.name))
            return out
        from dvp.services.connections import ConnectionService

        return [(c["id"], c["name"]) for c in ConnectionService(self.store, self.secrets).list(ctx)]

    def _resolve_schemas(
        self, ctx: TenantContext, connection_id: str, coords: dict, schema: str | None,
        schemas: list[str] | None, warnings: list[dict], cname: str,
    ) -> list[str]:
        if schemas:
            return schemas
        if schema:
            return [schema]
        res = self._catalog().list_schemas(ctx, connection_id)  # cached
        warnings.extend(res.get("warnings") or [])
        return res.get("schemas") or [coords.get("schema") or "main"]

    def _catalog(self):
        from dvp.services.catalog import CatalogService

        return CatalogService(self.store, self.secrets)
    def _match_columns(
        self, connector, dialect: str, ref_sql: str, cols: list[str], vals: list[str],
        *, co: bool, col_types: dict[str, str | None] | None = None,
    ) -> tuple[list[dict[str, int]], int | None]:
        """ONE aggregate query → per-(value, column) match counts AND the combined match_count
        (rows satisfying the any/co predicate). Folding the combined count in here saves the
        extra COUNT(*) scan `_build_hit` used to do. Predicates are sargable where the column
        type is known (`col_types`)."""
        types = col_types or {}

        def pred(col: str, val: str) -> str:
            return _pred(dialect, col, val, types.get(col))

        selects = [
            f"COUNT(CASE WHEN {pred(col, vals[i])} THEN 1 END) AS m_{i}_{j}"
            for i in range(len(vals))
            for j, col in enumerate(cols)
        ]
        combined = _combined_where(vals, cols, co, pred)  # over all candidate cols (0-contrib ok)
        selects.append(f"COUNT(CASE WHEN {combined} THEN 1 END) AS mc")
        sql = f"SELECT {', '.join(selects)} FROM {ref_sql}"  # noqa: S608 — idents from schema, values escaped
        res = connector.execute(sql)
        row = list(res.rows[0]) if res.rows else [0] * len(selects)
        out: list[dict[str, int]] = [{} for _ in vals]
        k = 0
        for i in range(len(vals)):
            for col in cols:
                cnt = int(row[k] or 0)
                if cnt > 0:
                    out[i][col] = cnt
                k += 1
        match_count = int(row[k] or 0)
        return out, match_count

    def _build_hit(
        self, connector, dialect: str, ref_sql: str, vals: list[str],
        matched: list[dict[str, int]], match_count: int | None, *, co: bool, limit: int,
        connection_id: str, connection_name: str, database: str, schema: str,
        table: str, warnings: list[dict], col_types: dict[str, str | None] | None = None,
    ) -> dict:
        types = col_types or {}
        columns = [
            {"value": vals[i], "column": col, "matches": cnt}
            for i in range(len(vals))
            for col, cnt in matched[i].items()
        ]
        where = _combined_where(
            vals, None, co, lambda col, val: _pred(dialect, col, val, types.get(col)), matched=matched,
        )
        sample = self._sample(connector, dialect, ref_sql, where, limit, table, warnings)
        return {
            "connection_id": connection_id,
            "connection_name": connection_name,
            "database": database or None,
            "schema": schema,
            "table": table,
            "match_count": match_count,
            "columns": columns,
            "sample": sample,
        }

    def _sample(
        self, connector, dialect: str, ref_sql: str, where: str, limit: int,
        table: str, warnings: list[dict],
    ) -> dict:
        try:
            res = connector.execute(_limited(dialect, f"SELECT * FROM {ref_sql} WHERE {where}", limit))  # noqa: S608
            rows = [[_json_safe(v) for v in r] for r in list(res.rows)[:limit]]
            return {"columns": list(res.columns), "rows": rows}
        except Exception as exc:  # noqa: BLE001
            warnings.append(_warn("sample_failed", f"{table}: {exc}"))
            return {"columns": [], "rows": []}

    def _list_tables(
        self, ctx: TenantContext, connection_id: str, schema: str, database: str | None,
        warnings: list[dict],
    ) -> list[str]:
        """Table names in a schema, served from the shared catalog cache (fast on repeat
        searches; the slow source listing happens only on a cache miss or explicit refresh)."""
        res = self._catalog().list_tables(ctx, connection_id, schema, database)
        warnings.extend(res.get("warnings") or [])
        return [t["name"] if isinstance(t, dict) else t for t in res.get("tables", [])]

    def _connector(self, ctx: TenantContext, connection_id: str):
        make, coords, name = self._connector_factory(ctx, connection_id)
        return make(), coords, name

    def _connector_factory(self, ctx: TenantContext, connection_id: str):
        """Return (make, coords, name) where make() builds a FRESH connector each call.

        We read the connection row + secret once, then hand back a thunk — so parallel confirms
        can give each worker thread its own connector (connectors/cursors aren't thread-safe)
        without re-hitting the metadata DB per build."""
        from dvp.connectors.registry import build_connector

        with self.store.session(ctx) as s:
            conn = s.get(Connection, connection_id)
            if conn is None or conn.group_id != ctx.group_id:
                raise ValidationError("connection not found", details={"id": connection_id})
            source_type, coords = conn.source_type, dict(conn.coordinates)
            auth, secret_ref, name = conn.auth_method, conn.secret_ref, conn.name
        secret = self.secrets.get(ctx, secret_ref) if (secret_ref and self.secrets) else None

        def make():
            return build_connector(source_type, coords, auth, secret)

        return make, coords, name


def _qcol(dialect: str, ident: str) -> str:
    if (dialect or "").lower() in ("mysql", "mariadb"):
        return "`" + ident.replace("`", "``") + "`"
    return '"' + ident.replace('"', '""') + '"'


def _text(dialect: str, expr: str) -> str:
    """Cast an expression to text so string equality works across column types."""
    d = (dialect or "").lower()
    if d == "oracle":
        return f"TO_CHAR({expr})"
    if d in ("snowflake", "bigquery", "redshift", "duckdb"):
        return f"CAST({expr} AS VARCHAR)"
    if d in ("mysql", "mariadb"):
        return f"CAST({expr} AS CHAR)"
    return f"CAST({expr} AS TEXT)"  # sqlite, postgres, default


def _lit(value: str) -> str:
    """A safely-escaped SQL string literal (single quotes doubled)."""
    return "'" + str(value).replace("'", "''") + "'"


_STRICT_NUM = re.compile(r"-?\d+(\.\d+)?")
# Dialects with real column typing where a native predicate is both correct and index-usable.
# SQLite (local) has NONE column affinity — a zip stored as text "27519" does not equal the
# numeric literal 27519 — so there we must keep the cross-type-safe CAST comparison.
_SARGABLE_DIALECTS = frozenset(
    {"snowflake", "oracle", "postgres", "postgresql", "mysql", "mariadb", "bigquery", "redshift", "duckdb"}
)


def _pred(dialect: str, column: str, value: str, col_tag: str | None = None) -> str:
    """An equality predicate for the confirm/sample query.

    On a typed warehouse we compare NATIVELY (no CAST on the column) so the source can use an
    index — `col = 27519` / `col = 'NC'` are sargable, whereas `CAST(col AS TEXT)=…` forces a
    full scan. We fall back to the CAST comparison for SQLite (untyped), unknown dialects, and
    date/mixed columns — always correct, just not sargable."""
    q = _qcol(dialect, column)
    if (dialect or "").lower() in _SARGABLE_DIALECTS:
        if col_tag == "numeric" and _STRICT_NUM.fullmatch(value):
            return f"{q} = {value}"  # digits/./- only → safe to inline, index-usable
        if col_tag == "string":
            return f"{q} = {_lit(value)}"  # native string compare, index-usable
    return f"{_text(dialect, q)} = {_lit(value)}"  # cross-type-safe fallback (not sargable)


def col_tag(is_numeric: bool, inferred_type: str | None) -> str | None:
    """Map a column's stored type to a predicate strategy for `_pred`."""
    if is_numeric:
        return "numeric"
    if inferred_type == "string":
        return "string"
    return None  # date / mixed / unknown → CAST fallback


def _combined_where(vals, cols, co, pred, matched=None) -> str:  # noqa: ANN001
    """Build the any/co-occurrence predicate. Over `matched` columns (confirm/sample) or all
    candidate `cols` (the folded count). co = AND across values of (OR across their columns)."""
    colsets = (
        [list(matched[i].keys()) for i in range(len(vals))] if matched is not None
        else [list(cols) for _ in range(len(vals))]
    )
    if co:
        if any(not cs for cs in colsets):
            return "1=0"
        return " AND ".join(
            "(" + " OR ".join(pred(c, vals[i]) for c in colsets[i]) + ")" for i in range(len(vals))
        )
    terms = [pred(c, vals[i]) for i in range(len(vals)) for c in colsets[i]]
    return " OR ".join(terms) if terms else "1=0"


def _limited(dialect: str, sql: str, limit: int) -> str:
    if (dialect or "").lower() == "oracle":
        return f"SELECT * FROM ({sql}) WHERE ROWNUM <= {limit}"
    return f"{sql} LIMIT {limit}"


def _json_safe(v):  # noqa: ANN001, ANN201
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return repr(v)
    return str(v)


def _warn(code: str, message: str) -> dict:
    return {"code": code, "message": message, "details": None}
