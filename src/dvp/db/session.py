"""MetadataStore — the persistence adapter.

Local mode: in-memory SQLite kept alive across threads via StaticPool (so the
schema persists for the life of the process). Server/prod: Postgres, where we also
set the RLS session GUCs per transaction (docs/design/02-multi-tenancy.md §3.1).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from dvp.config import Settings
from dvp.core.tenancy import TenantContext
from dvp.db.base import Base


class MetadataStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        url = settings.effective_database_url
        if url.startswith("sqlite") and ":memory:" not in url and url != "sqlite://":
            # File-backed SQLite (local mode) — ensure the parent directory exists so the
            # first boot can create the DB file.
            from pathlib import Path

            db_file = url.split("sqlite:///", 1)[-1]
            Path(db_file).parent.mkdir(parents=True, exist_ok=True)
            connect_args: dict = {"check_same_thread": False}
            self.engine: Engine = create_engine(url, connect_args=connect_args)
        elif url.startswith("sqlite"):
            # In-memory: one shared connection for all threads.
            self.engine = create_engine(
                "sqlite://",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        else:
            self.engine = create_engine(url, pool_pre_ping=True)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        # SQLite connections are not safe for concurrent use across threads. Under a real
        # server, FastAPI runs sync endpoints in a threadpool, so parallel requests (e.g.
        # the table page firing several fetches at once) would hit the shared in-memory
        # connection concurrently and segfault. Serialize DB access for SQLite; Postgres
        # (server/prod) relies on its own connection pool and needs no app-level lock.
        self._lock: threading.RLock | None = threading.RLock() if url.startswith("sqlite") else None

    def create_all(self) -> None:
        """Create schema. Used in local/test mode; server/prod uses Alembic."""
        Base.metadata.create_all(self.engine)
        if self.engine.dialect.name == "sqlite":
            # Local mode has no migration tool, and create_all never ALTERs existing tables.
            # Additively reconcile a persisted DB with the current models so adding a (nullable
            # or defaulted) column never breaks an existing .dvp/dvp.db.
            self._add_missing_sqlite_columns()
            self._relax_incident_monitor_id()

    def _add_missing_sqlite_columns(self) -> None:
        from sqlalchemy import inspect

        insp = inspect(self.engine)
        tables = set(insp.get_table_names())
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                if table.name not in tables:
                    continue
                have = {c["name"] for c in insp.get_columns(table.name)}
                for col in table.columns:
                    if col.name in have:
                        continue
                    coltype = col.type.compile(self.engine.dialect)
                    conn.exec_driver_sql(
                        f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'
                    )

    def _relax_incident_monitor_id(self) -> None:
        """A compare or a dbt run can open an incident with no monitor.

        create_all never drops NOT NULL, so an older local file still rejects that insert.
        """
        from sqlalchemy import inspect

        insp = inspect(self.engine)
        if "incident" not in set(insp.get_table_names()):
            return
        col = next((c for c in insp.get_columns("incident") if c["name"] == "monitor_id"), None)
        if col is None or col.get("nullable", True):
            return
        with self.engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE incident_new (
                    id VARCHAR(36) NOT NULL,
                    monitor_id VARCHAR(36),
                    dq_rule_id VARCHAR(36),
                    table_ref VARCHAR(512),
                    severity VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    summary TEXT,
                    sample_ref VARCHAR(512),
                    dedup_key VARCHAR(255),
                    dimension VARCHAR(32),
                    kind VARCHAR(32),
                    evidence JSON NOT NULL,
                    occurrences INTEGER NOT NULL,
                    resolved_at DATETIME,
                    org_id VARCHAR(36) NOT NULL,
                    group_id VARCHAR(36) NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    FOREIGN KEY(monitor_id) REFERENCES monitor (id),
                    FOREIGN KEY(dq_rule_id) REFERENCES dq_rule (id)
                )
                """
            )
            conn.exec_driver_sql(
                """
                INSERT INTO incident_new (
                    id, monitor_id, dq_rule_id, table_ref, severity, status, summary,
                    sample_ref, dedup_key, dimension, kind, evidence, occurrences,
                    resolved_at, org_id, group_id, created_at, updated_at
                )
                SELECT
                    id, monitor_id, dq_rule_id, table_ref, severity, status, summary,
                    sample_ref, dedup_key, dimension, kind, evidence, occurrences,
                    resolved_at, org_id, group_id, created_at, updated_at
                FROM incident
                """
            )
            conn.exec_driver_sql("DROP TABLE incident")
            conn.exec_driver_sql("ALTER TABLE incident_new RENAME TO incident")
            conn.exec_driver_sql("CREATE INDEX ix_incident_monitor_id ON incident (monitor_id)")
            conn.exec_driver_sql("CREATE INDEX ix_incident_group_id ON incident (group_id)")
            conn.exec_driver_sql("CREATE INDEX ix_incident_dedup_key ON incident (dedup_key)")
            conn.exec_driver_sql("CREATE INDEX ix_incident_org_id ON incident (org_id)")

    @contextmanager
    def session(self, ctx: TenantContext | None = None) -> Iterator[Session]:
        """Open a transactional session. When a context is given on Postgres, set
        the RLS GUCs so the DB enforces group isolation even if an app filter is
        missed."""
        with self._lock or nullcontext():
            session = self._session_factory()
            try:
                if ctx and self.settings.rls_enabled:
                    session.execute(text("SET LOCAL app.current_org = :org"), {"org": ctx.org_id})
                    session.execute(
                        text("SET LOCAL app.current_group = :grp"), {"grp": ctx.group_id}
                    )
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()


@event.listens_for(Engine, "connect")
def _sqlite_fk_pragma(dbapi_conn, _record) -> None:  # noqa: ANN001
    """Enable foreign keys on SQLite connections."""
    module = type(dbapi_conn).__module__
    if "sqlite" in module:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
