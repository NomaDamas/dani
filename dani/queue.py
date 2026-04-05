from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

from dani.models import JobRecord

logger = logging.getLogger(__name__)

JobHandler = Callable[[JobRecord], Any]

# Stages that touch git branches and require exclusive issue access.
_ISSUE_LOCKED_STAGES = frozenset({
    "implementation",
    "review_round",
    "final_verdict",
    "merge_conflict_resolution",
})

# Stages that end an issue's lifecycle and release the lock.
_TERMINAL_STAGES = frozenset({"final_verdict"})


class RepoQueueManager:
    def __init__(self, handler: JobHandler) -> None:
        self._handler = handler
        self._queues: dict[str, queue.Queue[JobRecord]] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        self._active_issue: dict[str, int] = {}
        self._deferred: dict[str, list[JobRecord]] = {}

    def submit(self, job: JobRecord) -> None:
        with self._lock:
            repo_queue = self._queues.setdefault(job.repo_full_name, queue.Queue())
            if job.repo_full_name not in self._threads:
                thread = threading.Thread(
                    target=self._worker,
                    args=(job.repo_full_name,),
                    daemon=True,
                    name=f"dani-worker-{job.repo_full_name.replace('/', '-')}",
                )
                self._threads[job.repo_full_name] = thread
                thread.start()
        repo_queue.put(job)

    def _worker(self, repo_full_name: str) -> None:
        repo_queue = self._queues[repo_full_name]
        while True:
            job = repo_queue.get()
            try:
                if self._try_execute_or_defer(repo_full_name, job):
                    try:
                        self._handler(job)
                    except Exception:
                        job.status = "failed"
                    finally:
                        try:
                            self._after_job(repo_full_name, job)
                        except Exception:
                            logger.exception("_after_job failed for job %s, force-releasing issue lock", job.id)
                            self._force_release(repo_full_name)
            finally:
                repo_queue.task_done()

    def _needs_issue_lock(self, job: JobRecord) -> bool:
        return job.issue_number is not None and job.stage in _ISSUE_LOCKED_STAGES

    def _try_execute_or_defer(self, repo: str, job: JobRecord) -> bool:
        """Return True if the job should execute now, False if deferred."""
        if not self._needs_issue_lock(job):
            return True
        with self._lock:
            active = self._active_issue.get(repo)
            issue = job.issue_number  # guaranteed non-None by _needs_issue_lock
            if active is None or active == issue:
                self._active_issue[repo] = issue  # type: ignore[assignment]
                return True
            self._deferred.setdefault(repo, []).append(job)
            return False

    def _after_job(self, repo: str, job: JobRecord) -> None:
        if not self._needs_issue_lock(job):
            return
        if job.stage in _TERMINAL_STAGES or job.status == "failed":
            self._flush_deferred(repo)

    def _flush_deferred(self, repo: str) -> None:
        with self._lock:
            self._active_issue.pop(repo, None)
            deferred = self._deferred.pop(repo, [])
        for deferred_job in deferred:
            self._queues[repo].put(deferred_job)

    def _force_release(self, repo: str) -> None:
        """Emergency release: clear active issue and re-enqueue deferred jobs."""
        with self._lock:
            self._active_issue.pop(repo, None)
            deferred = self._deferred.pop(repo, [])
        for deferred_job in deferred:
            self._queues[repo].put(deferred_job)

    def join_all(self) -> None:
        for repo_queue in self._queues.values():
            repo_queue.join()
        with self._lock:
            for repo, jobs in self._deferred.items():
                if jobs:
                    logger.warning("join_all: %d deferred jobs for %s were never flushed", len(jobs), repo)
