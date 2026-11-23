"""Process-level cache of live warehouse connections — the "auth once" guarantee.

Browser SSO (``authenticator=externalbrowser``) pops a web page on *every*
``sf.connect()``. A desktop POC opens many connections — two per compare (source +
target) and fresh ones per run — so without reuse the user gets a popup every time.

This pool keys live connections by identity (account / user / role / warehouse /
auth) and hands the SAME physical connection to every connector that matches, so
the browser opens ONCE per identity per process and every later compare/test
reuses it.

How this relates to the driver's own caching: ``client_store_temporary_credential``
makes the driver cache the SSO ID token *across processes*, but only when the
Snowflake account has ``ALLOW_ID_TOKEN = TRUE`` (an ACCOUNTADMIN setting). This pool
is the in-process guarantee that holds regardless of that account flag — the two
are complementary.
"""

from __future__ import annotations

import atexit
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class PoolEntry:
    """A shared physical connection plus the lock that serializes use of it.

    Multiple connector instances (and concurrent compares) may hold the same entry;
    the lock ensures only one query runs on the connection at a time, since a single
    Snowflake connection is not safe for concurrent cursor execution."""

    conn: Any
    lock: threading.RLock


class ConnectionPool:
    def __init__(self) -> None:
        self._entries: dict[tuple, PoolEntry] = {}
        self._guard = threading.Lock()

    def acquire(
        self,
        key: tuple,
        factory: Callable[[], Any],
        is_closed: Callable[[Any], bool],
    ) -> PoolEntry:
        """Return the live entry for ``key``, creating it once if absent.

        ``factory`` is where the browser popup happens — it runs at most once per key
        for as long as the connection stays open, so SSO is a one-time prompt. A stale
        (closed) connection is dropped and transparently rebuilt."""
        with self._guard:
            entry = self._entries.get(key)
            if entry is not None:
                try:
                    if not is_closed(entry.conn):
                        return entry
                except Exception:  # noqa: BLE001 — treat an unknowable state as stale
                    pass
                self._entries.pop(key, None)  # stale: rebuild below
            # factory() may block on an interactive browser login; holding the guard
            # here is deliberate — it collapses a concurrent double-request into one
            # popup instead of racing two logins for the same identity.
            conn = factory()
            entry = PoolEntry(conn=conn, lock=threading.RLock())
            self._entries[key] = entry
            return entry

    def evict(self, key: tuple) -> None:
        """Close and forget a connection — used when a session is found broken, so the
        next acquire re-establishes it (one fresh popup) rather than reusing a dead one."""
        with self._guard:
            entry = self._entries.pop(key, None)
        if entry is not None:
            try:
                entry.conn.close()
            except Exception:  # noqa: BLE001
                pass

    def close_all(self) -> None:
        """Close every pooled connection (process shutdown / test cleanup)."""
        with self._guard:
            entries = list(self._entries.values())
            self._entries.clear()
        for e in entries:
            try:
                e.conn.close()
            except Exception:  # noqa: BLE001
                pass


POOL = ConnectionPool()
atexit.register(POOL.close_all)
