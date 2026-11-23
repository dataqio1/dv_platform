"""SecretStore — connection credentials never touch the metadata DB.

Secrets are keyed by a path `dvp/{org}/{group}/connections/{conn}`. Reads assert
the caller's group matches the path, so a group can only resolve its own secrets
(docs/design/02-multi-tenancy.md §3.2). Local mode uses an in-memory/keyring store;
server/prod use Vault or a cloud secrets manager.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from dvp.core.errors import NotFoundError, PermissionDeniedError
from dvp.core.tenancy import TenantContext


def secret_path(ctx: TenantContext, connection_id: str) -> str:
    return f"dvp/{ctx.org_id}/{ctx.group_id}/connections/{connection_id}"


def _assert_scope(ctx: TenantContext, path: str) -> None:
    expected = f"dvp/{ctx.org_id}/{ctx.group_id}/"
    if not path.startswith(expected):
        raise PermissionDeniedError(
            "secret path is outside the caller's group scope",
            details={"path": path},
        )


class SecretStore(Protocol):
    def put(self, ctx: TenantContext, path: str, secret: dict) -> None: ...
    def get(self, ctx: TenantContext, path: str) -> dict: ...
    def delete(self, ctx: TenantContext, path: str) -> None: ...


class InMemorySecretStore:
    """Default for local/test mode. Nothing persisted to disk."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}

    def put(self, ctx: TenantContext, path: str, secret: dict) -> None:
        _assert_scope(ctx, path)
        self._store[path] = dict(secret)

    def get(self, ctx: TenantContext, path: str) -> dict:
        _assert_scope(ctx, path)
        if path not in self._store:
            raise NotFoundError("secret not found", details={"path": path})
        return dict(self._store[path])

    def delete(self, ctx: TenantContext, path: str) -> None:
        _assert_scope(ctx, path)
        self._store.pop(path, None)


class LocalFileSecretStore:
    """Desktop/local persistence: one JSON file per secret under a per-group directory,
    written 0600 so credentials survive restarts without a Vault/cloud secrets manager.
    Group-scoped (same isolation guard as reads)."""

    def __init__(self, base_dir: str) -> None:
        self.base = Path(base_dir)

    def _file(self, ctx: TenantContext, path: str) -> Path:
        _assert_scope(ctx, path)
        name = path.rsplit("/", 1)[-1]  # connection id
        return self.base / ctx.org_id / ctx.group_id / f"{name}.json"

    def put(self, ctx: TenantContext, path: str, secret: dict) -> None:
        f = self._file(ctx, path)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(secret), encoding="utf-8")
        os.chmod(f, 0o600)  # credentials are owner-only

    def get(self, ctx: TenantContext, path: str) -> dict:
        f = self._file(ctx, path)
        if not f.exists():
            raise NotFoundError("secret not found", details={"path": path})
        return json.loads(f.read_text(encoding="utf-8"))

    def delete(self, ctx: TenantContext, path: str) -> None:
        self._file(ctx, path).unlink(missing_ok=True)
