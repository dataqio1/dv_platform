"""Worker — claims queued compare jobs and runs each as its own OS process.

No broker: it polls the DB (`JobQueueStore`), launches `python -m dvp.workers.run_compare`
per job up to a concurrency cap, supervises them (timeout → kill + fail), and reclaims jobs
whose worker died. Run `dvp worker` as a separate process (server/prod) or embed it in `dvp
run` on the desktop. Scale out by starting more workers — they coordinate through the DB.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

from dvp.core.logging import get_logger
from dvp.workers.queue import JobQueueStore

log = get_logger("dvp.worker")


class Worker:
    def __init__(self, container, worker_id: str | None = None) -> None:  # noqa: ANN001
        self.settings = container.settings
        self.queue = JobQueueStore(container.metadata)
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        # job_id -> (Popen, monotonic_start)
        self._running: dict[str, tuple[subprocess.Popen, float]] = {}
        self._stop = False

    # ----- subprocess management -----
    def _spawn(self, job_id: str) -> subprocess.Popen:
        return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-m", "dvp.workers.run_compare", "--job-id", job_id],
            env=dict(os.environ),
        )

    def _reap(self) -> None:
        now = time.monotonic()
        for jid, (proc, started) in list(self._running.items()):
            rc = proc.poll()
            if rc is None:
                if now - started > self.settings.job_timeout_seconds:
                    proc.kill()
                    try:
                        proc.wait(timeout=10)
                    except Exception:  # noqa: BLE001
                        pass
                    self.queue.mark_failed(
                        jid, f"timed out after {self.settings.job_timeout_seconds}s"
                    )
                    log.warning("job_timeout", job=jid)
                    del self._running[jid]
                else:
                    self.queue.heartbeat(jid)
                continue
            # finished — run_compare's run_safe already recorded success/failure. Backstop a
            # non-zero exit that somehow left the row 'running' (e.g. the process was killed).
            if rc != 0:
                snap = self.queue.snapshot(jid)
                if snap and snap["status"] == "running":
                    self.queue.mark_failed(jid, f"worker subprocess exited {rc}")
            log.info("job_finished", job=jid, exit_code=rc)
            del self._running[jid]

    def _fill(self) -> int:
        launched = 0
        while len(self._running) < self.settings.max_concurrent_jobs:
            claimed = self.queue.claim_next(
                self.worker_id, per_group_cap=self.settings.max_concurrent_jobs
            )
            if claimed is None:
                break
            self._running[claimed["id"]] = (self._spawn(claimed["id"]), time.monotonic())
            launched += 1
            log.info("job_launched", job=claimed["id"], worker=self.worker_id)
        return launched

    # ----- loops -----
    def tick(self) -> None:
        self._reap()
        failed = self.queue.fail_stale(self.settings.job_timeout_seconds)
        if failed:
            log.warning("stale_jobs_failed", count=len(failed), jobs=failed)
        self._fill()

    def drain(self, timeout: float = 60.0) -> None:
        """Claim everything queued and wait for all subprocesses to finish (used by tests
        and one-shot runs)."""
        deadline = time.monotonic() + timeout
        self._fill()
        while self._running and time.monotonic() < deadline:
            time.sleep(0.1)
            self._reap()
            self._fill()

    def run_forever(self) -> None:
        log.info("worker_started", worker=self.worker_id, max=self.settings.max_concurrent_jobs)
        while not self._stop:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - a bad tick must not kill the worker
                log.warning("worker_tick_failed", error=str(exc))
            time.sleep(self.settings.worker_poll_seconds)

    def stop(self) -> None:
        self._stop = True
