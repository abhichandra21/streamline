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
    # (completed, total) for jobs that report progress; None when unknown.
    progress: tuple[int, int] | None = None

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
        self._completion_callbacks: list[Callable[[Job], None]] = []

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        label: str = "job",
        pass_job: bool = False,
        **kwargs: Any,
    ) -> str:
        """Run fn on a background thread.

        Set pass_job to hand the Job to fn as a `job` keyword so it can report
        progress back to pollers.
        """
        job_id = str(uuid.uuid4())
        job = Job(id=job_id, label=label, status="pending", started_at=time.time())
        with self._lock:
            self._jobs[job_id] = job
            self._trim()
        if pass_job:
            kwargs["job"] = job

        def _run() -> None:
            job.status = "running"
            callbacks: list[Callable[[Job], None]] = []
            try:
                job.result = fn(*args, **kwargs)
                job.status = "done"
            except SystemExit as exc:
                log.error("Job %s (%s) exited with status %s", job_id[:8], label, exc.code)
                job.error = f"Process exited with status {exc.code}"
                job.status = "error"
            except Exception as exc:
                log.exception("Job %s (%s) failed", job_id[:8], label)
                job.error = str(exc)
                job.status = "error"
            finally:
                job.finished_at = time.time()
                with self._lock:
                    callbacks = list(self._completion_callbacks)

            for callback in callbacks:
                try:
                    callback(job)
                except Exception:
                    log.exception("Job completion callback failed for %s (%s)", job_id[:8], label)

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

    def add_completion_callback(self, callback: Callable[[Job], None]) -> None:
        with self._lock:
            self._completion_callbacks.append(callback)

    def _trim(self) -> None:
        """Keep only the most recent completed jobs to prevent unbounded growth."""
        done = [j for j in self._jobs.values() if j.status in ("done", "error")]
        if len(done) > _MAX_COMPLETED:
            for j in sorted(done, key=lambda j: j.started_at)[: len(done) - _MAX_COMPLETED]:
                del self._jobs[j.id]


registry = JobRegistry()
