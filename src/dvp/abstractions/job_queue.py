"""JobQueue — orchestration of async work (monitor runs, compares).

Local mode runs jobs in-process; server/prod use Redis + Arq. Both honor the same submit/await
interface so engine code is identical. The local queue is RESOURCE-AWARE: a job is admitted only
when enough memory + vCPUs are free (each job declares its needs), so a burst of heavy compares
queues instead of OOM-ing the box.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Protocol


def detect_total_memory_gb(default: float = 8.0) -> float:
    """Host RAM in GB (Linux/macOS via sysconf), else a safe default."""
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3), 1)
    except (ValueError, OSError, AttributeError):
        return default


def detect_vcpus(default: int = 4) -> int:
    return os.cpu_count() or default


class JobHandle:
    def __init__(self, job_id: str, future: Future | None = None) -> None:
        self.job_id = job_id
        self._future = future

    def result(self, timeout: float | None = None) -> Any:
        if self._future is None:
            return None
        return self._future.result(timeout=timeout)


class JobQueue(Protocol):
    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> JobHandle: ...


class InProcessJobQueue:
    """Desktop/local: run jobs on a small thread pool, no Redis required. Accepts (and ignores)
    the resource hints so it's a drop-in for the resource-aware queue."""

    def __init__(self, max_workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="dvp-job")

    def submit(self, fn: Callable[..., Any], *args: Any, memory_gb: float = 0.0, vcpus: int = 1, **kwargs: Any) -> JobHandle:
        job_id = str(uuid.uuid4())
        future = self._pool.submit(fn, *args, **kwargs)
        return JobHandle(job_id, future)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)


class ResourceAwareQueue:
    """Admits a job only when enough MEMORY (GB) and vCPUs are free; otherwise it waits in a FIFO
    queue. A job's needs are declared at submit (memory from its per-test budget, vCPUs from its
    options). Resources are released on completion. Limits are adjustable at runtime so an admin
    can retune the pool without a restart. Thread-based — local/desktop; server/prod use Arq."""

    def __init__(self, total_memory_gb: float, max_vcpus: int, max_workers: int = 64) -> None:
        self._total_mem = max(0.5, float(total_memory_gb))
        self._max_cpu = max(1, int(max_vcpus))
        self._free_mem = self._total_mem
        self._free_cpu = self._max_cpu
        self._cv = threading.Condition()
        self._q: deque = deque()  # FIFO of (mem, cpu, fn, args, kwargs, future)
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="dvp-job")
        threading.Thread(target=self._dispatch, daemon=True, name="dvp-dispatch").start()

    # ---- admission loop ----
    def _dispatch(self) -> None:
        while True:
            with self._cv:
                while not self._q:
                    self._cv.wait()
                mem, cpu, fn, args, kwargs, fut = self._q[0]
                # Clamp a single job's needs to the whole pool so an oversized job still runs
                # (alone) rather than blocking the queue forever.
                need_mem = min(mem, self._total_mem)
                need_cpu = min(cpu, self._max_cpu)
                while not (self._free_mem >= need_mem and self._free_cpu >= need_cpu):
                    self._cv.wait()
                self._q.popleft()
                self._free_mem -= need_mem
                self._free_cpu -= need_cpu

            def run(fn=fn, args=args, kwargs=kwargs, fut=fut, need_mem=need_mem, need_cpu=need_cpu):  # noqa: ANN001
                try:
                    fut.set_result(fn(*args, **kwargs))
                except Exception as exc:  # noqa: BLE001 — propagate to the Future
                    fut.set_exception(exc)
                finally:
                    with self._cv:
                        self._free_mem += need_mem
                        self._free_cpu += need_cpu
                        self._cv.notify_all()

            self._pool.submit(run)

    def submit(self, fn: Callable[..., Any], *args: Any, memory_gb: float = 0.0, vcpus: int = 1, **kwargs: Any) -> JobHandle:
        fut: Future = Future()
        with self._cv:
            self._q.append((float(memory_gb or 0.0), int(vcpus or 1), fn, args, kwargs, fut))
            self._cv.notify_all()
        return JobHandle(str(uuid.uuid4()), fut)

    def set_limits(self, total_memory_gb: float | None = None, max_vcpus: int | None = None) -> None:
        """Retune the pool live (admin). Adjusts free capacity by the same delta so in-flight work
        is unaffected and the new ceiling applies to future admissions."""
        with self._cv:
            if total_memory_gb and total_memory_gb > 0:
                self._free_mem += float(total_memory_gb) - self._total_mem
                self._total_mem = float(total_memory_gb)
            if max_vcpus and max_vcpus > 0:
                self._free_cpu += int(max_vcpus) - self._max_cpu
                self._max_cpu = int(max_vcpus)
            self._cv.notify_all()

    def stats(self) -> dict:
        with self._cv:
            return {
                "total_memory_gb": round(self._total_mem, 1),
                "used_memory_gb": round(self._total_mem - self._free_mem, 2),
                "total_vcpus": self._max_cpu,
                "used_vcpus": self._max_cpu - self._free_cpu,
                "waiting_for_resources": len(self._q),
            }

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)
