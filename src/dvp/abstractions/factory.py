"""Builds the right adapter implementations for the active runtime mode and holds
them in a Container that the API/worker depend on. This is the single place where
`local` vs `server` vs `prod` is translated into concrete wiring.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dvp.abstractions.identity import (
    IdentityProvider,
    LocalNoPasswordIdentity,
    OidcIdentityProvider,
)
from dvp.abstractions.job_queue import (
    JobQueue,
    ResourceAwareQueue,
    detect_total_memory_gb,
    detect_vcpus,
)
from dvp.abstractions.notifier import LocalNotifier, Notifier
from dvp.abstractions.result_store import LocalFileResultStore, ResultStore
from dvp.abstractions.secret_store import InMemorySecretStore, SecretStore
from dvp.abstractions.spark import SparkSessionProvider
from dvp.config import (
    IdentityProviderKind,
    ResultBackend,
    SecretBackend,
    Settings,
)
from dvp.db.session import MetadataStore
from dvp.testing_engine.cancel import CancelRegistry


@dataclass
class Container:
    settings: Settings
    metadata: MetadataStore
    secrets: SecretStore
    results: ResultStore
    jobs: JobQueue
    spark: SparkSessionProvider
    notifier: Notifier
    cancel_registry: CancelRegistry = field(default_factory=CancelRegistry)
    # Job ids handed to the in-process worker pool and not yet finished — the "live" set the
    # queue bell reads so it shows only work actually in flight (never created-but-unrun jobs).
    # set.add/discard are atomic under the GIL, so no explicit lock is needed.
    active_runs: set = field(default_factory=set)
    identity: IdentityProvider | None = None  # set after tenant bootstrap in local mode


def _build_secret_store(settings: Settings) -> SecretStore:
    if settings.secret_backend == SecretBackend.FILE:
        from dvp.abstractions.secret_store import LocalFileSecretStore

        return LocalFileSecretStore(settings.secret_local_dir)
    if settings.secret_backend == SecretBackend.MEMORY:
        return InMemorySecretStore()
    # keyring/vault adapters: stubbed to in-memory for the scaffold.
    return InMemorySecretStore()


def _build_result_store(settings: Settings) -> ResultStore:
    if settings.result_backend == ResultBackend.LOCAL:
        return LocalFileResultStore(settings.result_local_dir)
    # object/snowflake adapters: stubbed to local for the scaffold (see D9).
    return LocalFileResultStore(settings.result_local_dir)


def build_container(settings: Settings) -> Container:
    metadata = MetadataStore(settings)
    # Resource-aware worker pool: admits jobs by free memory + vCPUs (auto-detected from the host
    # unless the admin set explicit limits). Falls back to total ceilings, never below one slot.
    total_mem = settings.worker_total_memory_gb or detect_total_memory_gb()
    max_cpu = settings.worker_max_vcpus or detect_vcpus()
    return Container(
        settings=settings,
        metadata=metadata,
        secrets=_build_secret_store(settings),
        results=_build_result_store(settings),
        jobs=ResourceAwareQueue(total_memory_gb=total_mem, max_vcpus=max_cpu),
        spark=SparkSessionProvider(),
        notifier=LocalNotifier(),
    )


def build_identity_provider(
    settings: Settings, *, local_user_id: str, local_org_id: str
) -> IdentityProvider:
    if settings.identity_provider == IdentityProviderKind.LOCAL_NOPASSWORD:
        return LocalNoPasswordIdentity(
            user_id=local_user_id, email=settings.local_user_email, org_id=local_org_id
        )
    if settings.identity_provider == IdentityProviderKind.LDAP:
        from dvp.abstractions.ldap import Ldap3Directory, LdapIdentityProvider

        directory = Ldap3Directory(
            url=settings.ldap_url or "",
            user_dn_template=settings.ldap_user_dn_template,
            group_base_dn=settings.ldap_group_base_dn,
        )
        return LdapIdentityProvider(directory)
    return OidcIdentityProvider(issuer="", client_id="")
