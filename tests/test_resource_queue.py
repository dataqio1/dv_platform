"""Resource-aware worker queue: admits jobs only when enough memory + vCPUs are free, queues the
rest, and releases on completion. Deterministic via events (no mocks)."""
from __future__ import annotations

import threading

from dvp.abstractions.job_queue import ResourceAwareQueue


def _job(started: threading.Event, release: threading.Event):
    started.set()
    release.wait(5)


def test_queues_until_memory_frees():
    q = ResourceAwareQueue(total_memory_gb=10, max_vcpus=8)
    a_started, a_rel = threading.Event(), threading.Event()
    b_started, b_rel = threading.Event(), threading.Event()

    h1 = q.submit(_job, a_started, a_rel, memory_gb=8)
    assert a_started.wait(2)
    # Second job needs 8 GB but only 2 are free → it must WAIT, not run.
    h2 = q.submit(_job, b_started, b_rel, memory_gb=8)
    assert not b_started.wait(0.3)
    st = q.stats()
    assert st["used_memory_gb"] == 8 and st["waiting_for_resources"] == 1

    a_rel.set(); h1.result(3)          # free the 8 GB → b is admitted
    assert b_started.wait(2)
    b_rel.set(); h2.result(3)
    assert q.stats()["used_memory_gb"] == 0


def test_queues_until_vcpus_free():
    q = ResourceAwareQueue(total_memory_gb=64, max_vcpus=2)
    a_started, a_rel = threading.Event(), threading.Event()
    b_started, b_rel = threading.Event(), threading.Event()

    h1 = q.submit(_job, a_started, a_rel, memory_gb=1, vcpus=2)  # takes both vCPUs
    assert a_started.wait(2)
    h2 = q.submit(_job, b_started, b_rel, memory_gb=1, vcpus=1)
    assert not b_started.wait(0.3)       # no free vCPU
    a_rel.set(); h1.result(3)
    assert b_started.wait(2)
    b_rel.set(); h2.result(3)


def test_raising_the_limit_admits_a_waiting_job():
    q = ResourceAwareQueue(total_memory_gb=8, max_vcpus=4)
    a_started, a_rel = threading.Event(), threading.Event()
    b_started, b_rel = threading.Event(), threading.Event()

    h1 = q.submit(_job, a_started, a_rel, memory_gb=6)
    assert a_started.wait(2)
    h2 = q.submit(_job, b_started, b_rel, memory_gb=6)  # 6 > 2 free → waits
    assert not b_started.wait(0.3)
    # Admin bumps the pool to 16 GB → now both fit → b is admitted without finishing a.
    q.set_limits(total_memory_gb=16)
    assert b_started.wait(2)
    a_rel.set(); b_rel.set(); h1.result(3); h2.result(3)


def test_oversized_job_runs_alone_not_stuck():
    q = ResourceAwareQueue(total_memory_gb=4, max_vcpus=4)
    started, rel = threading.Event(), threading.Event()
    # Needs more than the whole pool — clamped so it still runs (alone), never permanently stuck.
    h = q.submit(_job, started, rel, memory_gb=999)
    assert started.wait(2)
    rel.set(); h.result(3)
