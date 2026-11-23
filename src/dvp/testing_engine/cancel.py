"""Cancellation for in-flight compares.

The API thread requests a cancel; the worker thread is (usually) blocked inside a long
warehouse query. Two mechanisms, used together:

  1. A per-job flag the compare engine polls BETWEEN queries (cooperative stop — no new
     queries are issued).
  2. The engine registers the live connector(s) it's querying through; on cancel we also abort
     the CURRENTLY-running query on them (so a single huge checksum doesn't have to finish).

Shared on the Container (a singleton) so both threads see the same state.
"""

from __future__ import annotations

import threading


class CompareCancelled(Exception):
    """Raised inside the engine when a cancel was requested — propagates to the runner, which
    marks the job 'cancelled' (distinct from a failure)."""


class CancelRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled: set[str] = set()
        self._connectors: dict[str, list] = {}

    def request(self, job_id: str) -> None:
        """Flag the job AND abort its in-flight query (best effort)."""
        with self._lock:
            self._cancelled.add(job_id)
            conns = list(self._connectors.get(job_id, ()))
        for c in conns:  # cancel outside the lock — connector.cancel() may block briefly
            if hasattr(c, "cancel"):
                try:
                    c.cancel()
                except Exception:  # noqa: BLE001 - cancel is best effort
                    pass

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancelled

    def register(self, job_id: str, *conns) -> None:  # noqa: ANN002
        """Register the connectors a job is querying through, so a cancel can reach them."""
        with self._lock:
            self._connectors.setdefault(job_id, []).extend(c for c in conns if c is not None)

    def finish(self, job_id: str) -> None:
        """Release a job's state once it's done (whatever the outcome)."""
        with self._lock:
            self._cancelled.discard(job_id)
            self._connectors.pop(job_id, None)
