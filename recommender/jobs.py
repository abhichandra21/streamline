"""Minimal in-process background job runner for single-user homelab use.

No external dependencies — just threads. Designed for one concurrent user.
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("recommender.jobs")

_MAX_COMPLETED = 20


@dataclass
class Job:
    id: str
    label: str
    status: str  # "pending" | "running" | "done" | "error"
    started_at: float
    finished_at: float | None = None
    result: Any = None
    error: str | None = None

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return end - self.started_at

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return self.finished_at - self.started_at


class JobRegistry:
    """Thread-safe registry for background jobs."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(self, fn: Callable[..., Any], *args: Any, label: str = "job", **kwargs: Any) -> str:
        job_id = str(uuid.uuid4())
        job = Job(id=job_id, label=label, status="pending", started_at=time.time())
        with self._lock:
            self._jobs[job_id] = job
            self._trim()

        def _run() -> None:
            job.status = "running"
            try:
                job.result = fn(*args, **kwargs)
                job.status = "done"
            except Exception as exc:
                log.exception("Job %s (%s) failed", job_id[:8], label)
                job.error = str(exc)
                job.status = "error"
            finally:
                job.finished_at = time.time()

        t = threading.Thread(target=_run, daemon=True, name=f"job-{job_id[:8]}")
        t.start()
        return job_id

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def running_jobs(self) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if j.status in ("pending", "running")]

    def recent_jobs(self, n: int = 10) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)[:n]

    def _trim(self) -> None:
        """Keep only the most recent completed jobs to prevent unbounded growth."""
        done = [j for j in self._jobs.values() if j.status in ("done", "error")]
        if len(done) > _MAX_COMPLETED:
            for j in sorted(done, key=lambda j: j.started_at)[: len(done) - _MAX_COMPLETED]:
                del self._jobs[j.id]


registry = JobRegistry()
