import threading
import time

from dani.models import JobRecord
from dani.queue import RepoQueueManager


def test_queue_is_serial_per_repo_and_parallel_across_repos() -> None:
    records: list[tuple[str, str, float]] = []
    lock = threading.Lock()
    started = threading.Event()

    def handler(job: JobRecord) -> None:
        with lock:
            records.append((job.repo_full_name, "start", time.perf_counter()))
            if len([entry for entry in records if entry[1] == "start"]) >= 2:
                started.set()
        if job.repo_full_name == "acme/repo-a" and job.issue_number == 1:
            time.sleep(0.2)
        elif job.repo_full_name == "acme/repo-b":
            started.wait(timeout=1)
        with lock:
            records.append((job.repo_full_name, "end", time.perf_counter()))

    manager = RepoQueueManager(handler)
    manager.submit(JobRecord(repo_full_name="acme/repo-a", stage="issue_request", issue_number=1))
    manager.submit(JobRecord(repo_full_name="acme/repo-a", stage="implementation", issue_number=1))
    manager.submit(JobRecord(repo_full_name="acme/repo-b", stage="issue_request", issue_number=9))
    manager.join_all()

    repo_a_starts = [ts for repo, phase, ts in records if repo == "acme/repo-a" and phase == "start"]
    repo_a_ends = [ts for repo, phase, ts in records if repo == "acme/repo-a" and phase == "end"]
    repo_b_start = next(ts for repo, phase, ts in records if repo == "acme/repo-b" and phase == "start")

    assert repo_a_starts[1] >= repo_a_ends[0]
    assert repo_b_start < repo_a_ends[0]
