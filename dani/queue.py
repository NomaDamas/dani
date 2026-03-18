from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import Any

from dani.models import JobRecord

JobHandler = Callable[[JobRecord], Any]


class RepoQueueManager:
    def __init__(self, handler: JobHandler) -> None:
        self._handler = handler
        self._queues: dict[str, queue.Queue[JobRecord]] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()

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
                self._handler(job)
            finally:
                repo_queue.task_done()

    def join_all(self) -> None:
        for repo_queue in self._queues.values():
            repo_queue.join()
