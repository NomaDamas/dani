import logging
import threading
import time

import pytest

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


def test_different_issues_serialized() -> None:
    """Implementation jobs for different issues must not interleave.

    Issue #1's lifecycle (impl → final_verdict) must complete before issue #2's
    implementation starts.
    """
    execution_order: list[tuple[int, str]] = []
    lock = threading.Lock()

    def handler(job: JobRecord) -> None:
        with lock:
            execution_order.append((job.issue_number, job.stage))  # type: ignore[arg-type]
        # Simulate work
        time.sleep(0.05)
        # Simulate final_verdict marking the job as completed
        if job.stage == "final_verdict":
            job.status = "completed"

    manager = RepoQueueManager(handler)
    # Submit impl for issue #1, then impl for issue #2, then final_verdict for #1
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="implementation", issue_number=1))
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="implementation", issue_number=2))
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="final_verdict", issue_number=1))
    manager.join_all()

    # Issue #2's impl should come AFTER issue #1's final_verdict
    assert execution_order == [
        (1, "implementation"),
        (1, "final_verdict"),
        (2, "implementation"),
    ]


def test_dev_sync_bypasses_issue_lock() -> None:
    """dev_sync jobs (no issue_number) run even when an issue is active."""
    execution_order: list[str] = []
    lock = threading.Lock()
    impl_started = threading.Event()

    def handler(job: JobRecord) -> None:
        if job.stage == "implementation":
            impl_started.set()
            # Hold the lock for a bit so dev_sync arrives while active
            time.sleep(0.1)
        with lock:
            execution_order.append(job.stage)
        if job.stage == "final_verdict":
            job.status = "completed"

    manager = RepoQueueManager(handler)
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="implementation", issue_number=1))
    # Wait for implementation to start, then submit dev_sync
    impl_started.wait(timeout=1)
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="dev_sync"))
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="final_verdict", issue_number=1))
    manager.join_all()

    # dev_sync should run between implementation and final_verdict (not deferred)
    assert execution_order == ["implementation", "dev_sync", "final_verdict"]


def test_issue_request_not_locked() -> None:
    """issue_request does not require issue lock, so it runs even when another issue is active."""
    execution_order: list[tuple[int, str]] = []
    lock = threading.Lock()
    impl_started = threading.Event()

    def handler(job: JobRecord) -> None:
        if job.stage == "implementation" and job.issue_number == 1:
            impl_started.set()
            time.sleep(0.1)
        with lock:
            execution_order.append((job.issue_number, job.stage))  # type: ignore[arg-type]
        if job.stage == "final_verdict":
            job.status = "completed"

    manager = RepoQueueManager(handler)
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="implementation", issue_number=1))
    impl_started.wait(timeout=1)
    # issue_request for issue #2 should NOT be deferred
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="issue_request", issue_number=2))
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="final_verdict", issue_number=1))
    manager.join_all()

    assert execution_order == [
        (1, "implementation"),
        (2, "issue_request"),
        (1, "final_verdict"),
    ]


def test_failed_job_releases_lock() -> None:
    """A failed locked job releases the issue lock so other issues can proceed."""
    execution_order: list[tuple[int, str]] = []
    lock = threading.Lock()

    def handler(job: JobRecord) -> None:
        with lock:
            execution_order.append((job.issue_number, job.stage))  # type: ignore[arg-type]
        # Simulate failure for issue #1
        if job.issue_number == 1:
            job.status = "failed"

    manager = RepoQueueManager(handler)
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="implementation", issue_number=1))
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="implementation", issue_number=2))
    manager.join_all()

    # Issue #2 should run after #1 fails
    assert execution_order == [
        (1, "implementation"),
        (2, "implementation"),
    ]


def test_superseded_job_releases_lock() -> None:
    """A superseded locked job releases the issue lock so other issues can proceed."""
    execution_order: list[tuple[int, str, str]] = []
    lock = threading.Lock()

    def handler(job: JobRecord) -> None:
        with lock:
            execution_order.append((job.issue_number, job.stage, job.status))  # type: ignore[arg-type]
        if job.issue_number == 1:
            job.status = "superseded"

    manager = RepoQueueManager(handler)
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="implementation", issue_number=1))
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="implementation", issue_number=2))
    manager.join_all()

    assert execution_order == [
        (1, "implementation", "queued"),
        (2, "implementation", "queued"),
    ]


def test_handler_exception_releases_lock() -> None:
    """If the handler raises an unhandled exception, the issue lock is still released."""
    execution_order: list[tuple[int, str]] = []
    lock = threading.Lock()

    def handler(job: JobRecord) -> None:
        with lock:
            execution_order.append((job.issue_number, job.stage))  # type: ignore[arg-type]
        if job.issue_number == 1:
            msg = "unexpected storage failure"
            raise RuntimeError(msg)

    manager = RepoQueueManager(handler)
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="implementation", issue_number=1))
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="implementation", issue_number=2))
    manager.join_all()

    # Issue #2 must still run even though #1's handler raised
    assert execution_order == [
        (1, "implementation"),
        (2, "implementation"),
    ]


def test_deferred_jobs_requeued_on_terminal() -> None:
    """When active issue reaches final_verdict, deferred jobs are re-enqueued.

    Each issue must also reach final_verdict before the next one starts.
    """
    execution_order: list[tuple[int, str]] = []
    lock = threading.Lock()

    def handler(job: JobRecord) -> None:
        with lock:
            execution_order.append((job.issue_number, job.stage))  # type: ignore[arg-type]
        if job.stage == "final_verdict":
            job.status = "completed"

    manager = RepoQueueManager(handler)
    # Issue #1 lifecycle
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="implementation", issue_number=1))
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="final_verdict", issue_number=1))
    # Issue #2 lifecycle (will be deferred until #1 finishes)
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="implementation", issue_number=2))
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="final_verdict", issue_number=2))
    # Issue #3 lifecycle (will be deferred until #2 finishes)
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="implementation", issue_number=3))
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="final_verdict", issue_number=3))
    manager.join_all()

    assert execution_order == [
        (1, "implementation"),
        (1, "final_verdict"),
        (2, "implementation"),
        (2, "final_verdict"),
        (3, "implementation"),
        (3, "final_verdict"),
    ]


def test_join_all_warns_on_orphaned_deferred_jobs(caplog: pytest.LogCaptureFixture) -> None:
    """join_all logs a warning when deferred jobs remain unflushed."""

    def handler(job: JobRecord) -> None:
        pass  # implementation completes but never reaches final_verdict

    manager = RepoQueueManager(handler)
    # Issue #1 takes the lock, issue #2 gets deferred
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="implementation", issue_number=1))
    manager.submit(JobRecord(repo_full_name="acme/repo", stage="implementation", issue_number=2))

    with caplog.at_level(logging.WARNING, logger="dani.queue"):
        manager.join_all()

    assert any("deferred jobs" in record.message for record in caplog.records)
