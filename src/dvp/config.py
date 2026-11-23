"""Runtime configuration & mode selection.

One env var (`DVP_PROFILE`) picks the runtime mode; the adapter factory
(dvp.abstractions.factory) reads these settings to wire the right implementations.
See docs/design/01-architecture.md §5 (Deployment & runtime modes).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeMode(StrEnum):
    LOCAL = "local"
    SERVER = "server"
    PROD = "prod"


class SecretBackend(StrEnum):
    MEMORY = "memory"
    KEYRING = "keyring"
    FILE = "file"
    VAULT = "vault"


class ResultBackend(StrEnum):
    LOCAL = "local"
    OBJECT = "object"
    SNOWFLAKE = "snowflake"


class IdentityProviderKind(StrEnum):
    LOCAL_NOPASSWORD = "local_nopassword"
    OIDC = "oidc"
    LDAP = "ldap"  # each LDAP group is a tenant (JIT-provisioned)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DVP_", env_file=".env", extra="ignore", case_sensitive=False
    )

    profile: RuntimeMode = RuntimeMode.LOCAL

    # Metadata store. In local mode this defaults to a file-backed SQLite at
    # `local_db_path` so data survives restarts; set DVP_DATABASE_URL to override
    # (e.g. "sqlite://" for an ephemeral in-memory DB, as the tests do).
    database_url: str | None = None
    local_db_path: str = "./.dvp/dvp.db"

    # Adapter backends (defaults are mode-appropriate; see effective_* below).
    # Desktop default: file-backed secrets so connection credentials persist across restarts
    # (0600 files under secret_local_dir). Tests override to "memory"; server/prod use vault.
    secret_backend: SecretBackend = SecretBackend.FILE
    secret_local_dir: str = "./.dvp/secrets"
    result_backend: ResultBackend = ResultBackend.LOCAL
    result_local_dir: str = "./.dvp/results"
    identity_provider: IdentityProviderKind = IdentityProviderKind.LOCAL_NOPASSWORD

    # LDAP multi-tenancy: each LDAP group becomes a tenant (JIT-provisioned on login).
    ldap_url: str | None = None
    ldap_user_dn_template: str = "uid={username},ou=people,dc=example,dc=com"
    ldap_group_base_dn: str = "ou=groups,dc=example,dc=com"
    # Which LDAP groups are tenants (empty = every non-role group the user is in). Role
    # groups below are never tenants — they only confer a capability level. A user's role is
    # the strongest conferred by their role groups, else `ldap_default_role`.
    ldap_tenant_groups: list[str] = []
    ldap_admin_groups: list[str] = []  # → GROUP_ADMIN (configure connections, members)
    ldap_editor_groups: list[str] = []  # → EDITOR (create/run test cases, not connections)
    ldap_viewer_groups: list[str] = []  # → VIEWER (read-only)
    ldap_report_viewer_groups: list[str] = []  # → REPORT_VIEWER (reports only)
    ldap_default_role: str = "editor"

    # API
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    log_level: str = "INFO"
    # Uvicorn event loop / HTTP implementation. "auto" lets uvicorn pick (uvloop +
    # httptools). uvloop has segfaulted under load on some macOS/Python builds, so
    # local mode falls back to the stable asyncio/h11 stack unless overridden here.
    uvicorn_loop: str = "auto"  # auto | asyncio | uvloop
    uvicorn_http: str = "auto"  # auto | h11 | httptools

    # Background scheduler: periodically run active monitors (profile + evaluate rules).
    # Off by default; opt in with DVP_SCHEDULER_ENABLED=true (local mode).
    scheduler_enabled: bool = False
    scheduler_interval_seconds: int = 300

    # Worker tier: bounded concurrency for async test-case runs so many submissions never
    # overwhelm the API/control plane. Local = in-process pool; server = Redis+Arq workers.
    max_concurrent_runs: int = 4

    # Resource-aware scheduling: the worker tier admits a job only when enough MEMORY and vCPUs
    # are free (a job's memory need comes from its per-test memory budget), so a burst of heavy
    # compares queues instead of OOM-ing the box. 0 = auto-detect the host's RAM / CPU count.
    # The admin can override these live (Administration → Compute).
    worker_total_memory_gb: float = 0.0
    worker_max_vcpus: int = 0

    # Heavy compare execution (cross-source data compares).
    #   thread     — in-process thread pool (desktop / light jobs; default, zero deps).
    #   subprocess — the DB is the queue and a `dvp worker` runs each compare as its OWN
    #                OS process (isolation, independent Spark). No Redis, no K8s required.
    compare_executor: str = "thread"  # thread | subprocess
    # Spark target for a compare subprocess — just a master URL, so scale is a config change:
    #   local[*] (one box) · spark://host:7077 (standalone) · sc://host (Connect) · k8s://… .
    spark_master: str = "local[*]"
    worker_poll_seconds: float = 2.0
    max_concurrent_jobs: int = 2  # concurrent compare subprocesses per worker
    job_timeout_seconds: int = 3600  # a run exceeding this is killed + requeued
    # Desktop convenience: run an embedded worker thread inside `dvp run` so the out-of-process
    # executor works with a single command (server/prod run `dvp worker` separately).
    embed_worker: bool = True

    # Local bootstrap identity (desktop / no-password mode).
    local_org_name: str = Field(default="Local Org")
    local_group_name: str = Field(default="Default Group")
    local_user_email: str = Field(default="local@dv-platform.dev")

    @property
    def is_local(self) -> bool:
        return self.profile == RuntimeMode.LOCAL

    @property
    def effective_database_url(self) -> str:
        """Local mode persists to a file-backed SQLite so data survives restarts (zero
        external deps). An explicit DVP_DATABASE_URL overrides it — e.g. "sqlite://" for an
        ephemeral in-memory DB (tests)."""
        if self.is_local:
            if self.database_url:
                return self.database_url
            from pathlib import Path

            return f"sqlite:///{Path(self.local_db_path).expanduser().resolve()}"
        if not self.database_url:
            raise ValueError(
                "DVP_DATABASE_URL is required in server/prod mode "
                "(e.g. postgresql+psycopg://user:pass@host/db)"
            )
        return self.database_url

    @property
    def rls_enabled(self) -> bool:
        """Row-Level Security is a Postgres feature; local SQLite relies on
        app-layer filtering only (docs/design/02-multi-tenancy.md §8)."""
        return not self.is_local and self.effective_database_url.startswith("postgresql")


@lru_cache
def get_settings() -> Settings:
    return Settings()
