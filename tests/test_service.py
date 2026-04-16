import threading
from pathlib import Path
from typing import cast

import pytest

from dani.github import GitHubCLI
from dani.models import DaniConfig, JobRecord, NormalizedEvent
from dani.omx_runner import OmxRunner
from dani.service import DaniService
from dani.signatures import build_signature
from dani.storage import JsonStorage
from tests.helpers import FakeGitDevSyncer, FakeGitHubCLI, FakeOmxRunner

TEST_SECRET = "unit-test-secret"


def add_exact_review_signature(github: FakeGitHubCLI, job: JobRecord) -> None:
    signature_fields: dict[str, str | int] = {
        "stage": "review_round",
        "job": job.id,
        "pr": int(job.pr_number or 0),
        "round": job.review_round or 1,
    }
    if job.issue_number is not None:
        signature_fields["issue"] = int(job.issue_number)
    github.add_pr_signature(
        job.repo_full_name,
        int(job.pr_number or 0),
        build_signature(**signature_fields),
    )


def make_service(
    tmp_path: Path, *, dev_syncer: FakeGitDevSyncer | None = None
) -> tuple[DaniService, FakeGitHubCLI, FakeOmxRunner]:
    class ExactReviewSignatureOmxRunner(FakeOmxRunner):
        def launch(self, repo_path: Path, job: JobRecord, prompt: str):
            session = super().launch(repo_path, job, prompt)
            if job.stage == "review_round" and job.issue_number is not None:
                add_exact_review_signature(self.github, job)
            return session

    config = DaniConfig(data_dir=tmp_path / ".dani", webhook_secret=TEST_SECRET)
    storage = JsonStorage(config)
    github = FakeGitHubCLI()
    omx_runner = ExactReviewSignatureOmxRunner(github)
    service = DaniService(
        config,
        storage=storage,
        github=cast(GitHubCLI, github),
        omx_runner=cast(OmxRunner, omx_runner),
        dev_syncer=dev_syncer or FakeGitDevSyncer(),
    )
    service.register_repo("acme/demo", str(tmp_path))
    return service, github, omx_runner


def make_pr_event(
    *,
    pr_number: int,
    action: str,
    body: str,
    actor_login: str = "contributor",
    commit_sha: str | None = None,
    delivery_id: str | None = None,
) -> NormalizedEvent:
    head_sha = commit_sha or f"sha-{pr_number}-{action}"
    return NormalizedEvent(
        kind="pull_request_opened",
        repo_full_name="acme/demo",
        action=action,
        number=pr_number,
        actor_login=actor_login,
        payload={"pull_request": {"head": {"sha": head_sha}}},
        body=body,
        title=f"Feature/#{pr_number}",
        base_branch="dev",
        head_branch=f"feature/#{pr_number}",
        commit_sha=head_sha,
        is_pull_request=True,
        delivery_id=delivery_id,
    )


def make_pr_comment_event(*, pr_number: int, body: str, actor_login: str = "agent") -> NormalizedEvent:
    return NormalizedEvent(
        kind="pull_request_comment",
        repo_full_name="acme/demo",
        action="created",
        number=pr_number,
        actor_login=actor_login,
        payload={},
        body=body,
        title=f"Feature/#{pr_number}",
        is_pull_request=True,
    )


def test_issue_request_persists_omx_session_id(tmp_path: Path) -> None:
    service, _, _ = make_service(tmp_path)

    service.handle_event(
        NormalizedEvent(
            kind="issue_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=21,
            actor_login="human",
            payload={},
            body="Need automation",
            title="Need automation",
        )
    )
    service.wait_for_idle()

    session = service.storage.list_sessions()[0]
    assert session.omx_session_id == "omx-" + session.job_id


def test_issue_opened_queues_issue_request(tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)
    event = NormalizedEvent(
        kind="issue_opened",
        repo_full_name="acme/demo",
        action="opened",
        number=11,
        actor_login="human",
        payload={},
        body="Need automation",
        title="Need automation",
    )

    result = service.handle_event(event)
    service.wait_for_idle()

    assert result["status"] == "queued"
    assert omx_runner.launches[0]["job"].stage == "issue_request"
    assert service.storage.list_jobs()[0].status == "completed"
    assert omx_runner.closed_sessions == [f"runtime-{service.storage.list_jobs()[0].id}"]
    session = service.storage.list_sessions()[0]
    assert session.status == "completed"
    assert session.ended_at is not None
    assert session.termination_reason == "completed"


def test_general_issue_comment_resumes_existing_issue_session(tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)
    service.handle_event(
        NormalizedEvent(
            kind="issue_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=31,
            actor_login="human",
            payload={},
            body="Need automation",
            title="Need automation",
        )
    )
    service.wait_for_idle()

    result = service.handle_event(
        NormalizedEvent(
            kind="issue_comment",
            repo_full_name="acme/demo",
            action="created",
            number=31,
            actor_login="human",
            payload={"issue": {"body": "Need automation"}},
            body="Please reconsider the edge cases.",
            title="Need automation",
        )
    )
    service.wait_for_idle()

    assert result["stage"] == "issue_followup"
    assert omx_runner.resumes[-1]["omx_session_id"].startswith("omx-")
    assert omx_runner.resumes[-1]["job"].stage == "issue_followup"
    followup_jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="issue_followup", issue_number=31)
    assert len(followup_jobs) == 1


def test_general_issue_comment_without_existing_issue_session_is_ignored(tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)

    result = service.handle_event(
        NormalizedEvent(
            kind="issue_comment",
            repo_full_name="acme/demo",
            action="created",
            number=32,
            actor_login="human",
            payload={"issue": {"body": "Need automation"}},
            body="Please reconsider the edge cases.",
            title="Need automation",
        )
    )

    assert result == {"status": "ignored", "reason": "missing_issue_session"}
    assert omx_runner.resumes == []


def test_approve_comment_queues_implementation(tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)
    event = NormalizedEvent(
        kind="issue_comment",
        repo_full_name="acme/demo",
        action="created",
        number=11,
        actor_login="human",
        payload={"issue": {"body": "context"}},
        body="/approve",
        title="Need automation",
    )

    result = service.handle_event(event)
    service.wait_for_idle()

    assert result["stage"] == "implementation"
    assert omx_runner.launches[0]["job"].stage == "implementation"
    assert service.storage.list_jobs()[0].status == "completed"


def test_failed_job_still_closes_runtime_handle_and_marks_failure(tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)
    session = omx_runner.launch(Path(tmp_path), JobRecord(repo_full_name="acme/demo", stage="implementation"), "")
    service.storage.create_session(session)

    service._finalize_session(session, status="failed", termination_reason="RuntimeError")

    stored = service.storage.list_sessions()[0]
    assert stored.status == "failed"
    assert stored.ended_at is not None
    assert stored.termination_reason == "RuntimeError"
    assert omx_runner.closed_sessions == [session.runtime_handle]


def test_pr_opened_from_implementation_signature_queues_review_round(tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)
    implementation_event = NormalizedEvent(
        kind="issue_comment",
        repo_full_name="acme/demo",
        action="created",
        number=12,
        actor_login="human",
        payload={"issue": {"body": "Ship it"}},
        body="/approve",
        title="Ship it",
    )
    service.handle_event(implementation_event)
    service.wait_for_idle()
    implementation_job = service.storage.find_jobs(repo_full_name="acme/demo", stage="implementation", issue_number=12)[
        0
    ]

    pr_event = NormalizedEvent(
        kind="pull_request_opened",
        repo_full_name="acme/demo",
        action="opened",
        number=99,
        actor_login="agent",
        payload={},
        body=f"Implements #12\n{build_signature(stage='implementation', job=implementation_job.id, issue=12)}",
        title="Feature/#12",
        base_branch="dev",
        head_branch="Feature/#12",
        is_pull_request=True,
    )

    result = service.handle_event(pr_event)
    service.wait_for_idle()

    review_jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=99)
    assert result["stage"] == "review_round"
    assert review_jobs[0].review_round == 1
    assert omx_runner.launches[-1]["job"].stage == "review_round"


def test_external_pr_opened_queues_review_round(tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)

    result = service.handle_event(make_pr_event(pr_number=88, action="opened", body="Implements #21"))
    service.wait_for_idle()

    assert result["stage"] == "review_round"
    review_jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=88)
    assert [job.review_round for job in review_jobs] == [1]
    assert review_jobs[0].metadata["external_contribution"] is True
    assert omx_runner.launches[-1]["job"].stage == "review_round"


def test_external_pr_new_commit_queues_another_review_round(tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)

    service.handle_event(make_pr_event(pr_number=88, action="opened", body="Implements #21"))
    service.wait_for_idle()

    result = service.handle_event(make_pr_event(pr_number=88, action="synchronize", body="Implements #21"))
    service.wait_for_idle()

    assert result["stage"] == "review_round"
    review_jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=88)
    assert [job.review_round for job in review_jobs] == [1, 2]
    assert omx_runner.launches[-1]["job"].stage == "review_round"


def test_duplicate_external_pr_activity_event_is_ignored(tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)

    service.handle_event(make_pr_event(pr_number=88, action="opened", body="Implements #21", commit_sha="sha-1"))
    service.wait_for_idle()

    first = service.handle_event(
        make_pr_event(pr_number=88, action="synchronize", body="Implements #21", commit_sha="sha-2")
    )
    service.wait_for_idle()
    duplicate = service.handle_event(
        make_pr_event(pr_number=88, action="synchronize", body="Implements #21", commit_sha="sha-2")
    )
    service.wait_for_idle()

    assert first["stage"] == "review_round"
    assert duplicate == {"status": "ignored", "reason": "duplicate_external_pr_event"}
    review_jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=88)
    assert [job.review_round for job in review_jobs] == [1, 2]
    assert len(omx_runner.launches) == 2


def test_duplicate_external_pr_activity_does_not_consume_review_limit(tmp_path: Path) -> None:
    service, _, _ = make_service(tmp_path)
    for round_number in range(1, 10):
        service.storage.create_job(
            JobRecord(
                repo_full_name="acme/demo",
                stage="review_round",
                pr_number=88,
                review_round=round_number,
                metadata={"external_contribution": True},
                status="completed",
            )
        )

    first = service.handle_event(
        make_pr_event(pr_number=88, action="synchronize", body="Implements #21", commit_sha="sha-10")
    )
    service.wait_for_idle()
    duplicate = service.handle_event(
        make_pr_event(pr_number=88, action="synchronize", body="Implements #21", commit_sha="sha-10")
    )
    service.wait_for_idle()

    assert first["stage"] == "review_round"
    assert duplicate == {"status": "ignored", "reason": "duplicate_external_pr_event"}
    review_jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=88)
    assert [job.review_round for job in review_jobs if job.metadata.get("external_contribution")] == list(range(1, 11))
    assert service.storage.find_jobs(repo_full_name="acme/demo", stage="human_escalation", pr_number=88) == []


def test_external_pr_fallback_dedupe_key_is_repo_scoped(tmp_path: Path) -> None:
    service, _, _ = make_service(tmp_path)
    service.register_repo("acme/other", str(tmp_path))

    first = service.handle_event(
        NormalizedEvent(
            kind="pull_request_opened",
            repo_full_name="acme/demo",
            action="synchronize",
            number=88,
            actor_login="contributor",
            payload={"pull_request": {"head": {"sha": "shared-sha"}}},
            body="Implements #21",
            title="Feature/#21",
            base_branch="dev",
            head_branch="feature/#21",
            commit_sha="shared-sha",
            is_pull_request=True,
        )
    )
    service.wait_for_idle()

    second = service.handle_event(
        NormalizedEvent(
            kind="pull_request_opened",
            repo_full_name="acme/other",
            action="synchronize",
            number=88,
            actor_login="contributor",
            payload={"pull_request": {"head": {"sha": "shared-sha"}}},
            body="Implements #21",
            title="Feature/#21",
            base_branch="dev",
            head_branch="feature/#21",
            commit_sha="shared-sha",
            is_pull_request=True,
        )
    )
    service.wait_for_idle()

    assert first["stage"] == "review_round"
    assert second["stage"] == "review_round"
    assert [
        job.review_round for job in service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round")
    ] == [1]
    assert [
        job.review_round for job in service.storage.find_jobs(repo_full_name="acme/other", stage="review_round")
    ] == [1]


def test_external_pr_review_requested_queues_review_round(tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)

    result = service.handle_event(make_pr_event(pr_number=91, action="review_requested", body="Implements #21"))
    service.wait_for_idle()

    assert result["stage"] == "review_round"
    assert omx_runner.launches[-1]["job"].stage == "review_round"


def test_external_review_comment_does_not_queue_implementation(tmp_path: Path) -> None:
    service, github, omx_runner = make_service(tmp_path)
    service.handle_event(make_pr_event(pr_number=88, action="opened", body="Implements #21"))
    service.wait_for_idle()
    review_job = service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=88)[-1]

    result = service.handle_event(
        make_pr_comment_event(
            pr_number=88,
            body=build_signature(stage="review_round", job=review_job.id, pr=88, round=1),
        )
    )
    service.wait_for_idle()

    assert result == {"status": "updated", "stage": "review_round"}
    assert service.storage.find_jobs(repo_full_name="acme/demo", stage="implementation", pr_number=88) == []
    assert len(omx_runner.launches) == 1
    assert github.merged == []


def test_external_review_approve_comment_queues_final_verdict(tmp_path: Path) -> None:
    service, github, omx_runner = make_service(tmp_path)
    service.handle_event(make_pr_event(pr_number=88, action="opened", body="Implements #21"))
    service.wait_for_idle()
    review_job = service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=88)[-1]

    result = service.handle_event(
        make_pr_comment_event(
            pr_number=88,
            body=f"/approve\n{build_signature(stage='review_round', job=review_job.id, pr=88, round=1)}",
        )
    )
    service.wait_for_idle()

    assert result["stage"] == "final_verdict"
    assert service.storage.find_jobs(repo_full_name="acme/demo", stage="implementation", pr_number=88) == []
    assert len(service.storage.find_jobs(repo_full_name="acme/demo", stage="final_verdict", pr_number=88)) == 1
    assert omx_runner.launches[-1]["job"].stage == "final_verdict"
    assert github.merged == []


def test_external_final_verdict_approve_merges_without_implementation_job(tmp_path: Path) -> None:
    service, github, omx_runner = make_service(tmp_path)

    result = service.handle_event(
        make_pr_comment_event(
            pr_number=88,
            body=build_signature(stage="final_verdict", job="verdict-1", pr=88, verdict="APPROVE"),
        )
    )
    service.wait_for_idle()

    assert result == {"status": "merged", "pr_number": 88}
    assert service.storage.find_jobs(repo_full_name="acme/demo", stage="implementation", pr_number=88) == []
    assert omx_runner.launches == []
    assert github.merged == [("acme/demo", 88)]


def test_external_pr_escalates_after_ten_review_passes(tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)
    for _ in range(10):
        service.storage.create_job(
            JobRecord(
                repo_full_name="acme/demo",
                stage="review_round",
                pr_number=88,
                metadata={"external_contribution": True},
                status="completed",
            )
        )

    result = service.handle_event(make_pr_event(pr_number=88, action="synchronize", body="Implements #21"))
    service.wait_for_idle()

    assert result["stage"] == "human_escalation"
    escalation_jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="human_escalation", pr_number=88)
    assert len(escalation_jobs) == 1
    assert omx_runner.launches[-1]["job"].stage == "human_escalation"


def test_external_pr_unique_activity_never_queues_an_eleventh_automated_review(tmp_path: Path) -> None:
    class BlockingOmxRunner(FakeOmxRunner):
        def __init__(self, github: FakeGitHubCLI) -> None:
            super().__init__(github)
            self.review_started = threading.Event()
            self.release_review = threading.Event()

        def launch(self, repo_path: Path, job: JobRecord, prompt: str):
            session = super().launch(repo_path, job, prompt)
            if job.stage == "review_round":
                add_exact_review_signature(self.github, job)
            return session

        def wait(self, runtime_handle: str, *, poll_interval: float = 0.5, timeout_seconds: float = 1800) -> None:
            if (
                runtime_handle.startswith("runtime-")
                and self.launches
                and self.launches[-1]["job"].stage == "review_round"
            ):
                self.review_started.set()
                self.release_review.wait(timeout=timeout_seconds)

    config = DaniConfig(data_dir=tmp_path / ".dani", webhook_secret=TEST_SECRET)
    storage = JsonStorage(config)
    github = FakeGitHubCLI()
    omx_runner = BlockingOmxRunner(github)
    service = DaniService(
        config,
        storage=storage,
        github=cast(GitHubCLI, github),
        omx_runner=cast(OmxRunner, omx_runner),
        dev_syncer=FakeGitDevSyncer(),
    )
    service.register_repo("acme/demo", str(tmp_path))

    opened = service.handle_event(
        make_pr_event(pr_number=88, action="opened", body="Implements #21", commit_sha="sha-1")
    )
    assert opened["stage"] == "review_round"
    assert omx_runner.review_started.wait(timeout=1)

    results = []
    for round_number in range(2, 12):
        results.append(
            service.handle_event(
                make_pr_event(
                    pr_number=88,
                    action="synchronize",
                    body="Implements #21",
                    commit_sha=f"sha-{round_number}",
                )
            )
        )

    assert all(result["stage"] == "review_round" for result in results[:9])
    assert results[9] == {"status": "ignored", "reason": "external_review_limit_pending"}
    assert service.storage.find_jobs(repo_full_name="acme/demo", stage="human_escalation", pr_number=88) == []

    omx_runner.release_review.set()
    service.wait_for_idle()

    review_jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=88)
    assert [job.review_round for job in review_jobs] == list(range(1, 11))
    assert all(job.status == "completed" for job in review_jobs)

    post_limit = service.handle_event(
        make_pr_event(
            pr_number=88,
            action="synchronize",
            body="Implements #21",
            commit_sha="sha-12",
        )
    )
    service.wait_for_idle()

    assert post_limit["stage"] == "human_escalation"
    assert service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=88) == review_jobs
    assert [job.review_round for job in review_jobs] == list(range(1, 11))


def test_review_chain_reaches_verdict_and_merges_on_approve(tmp_path: Path) -> None:
    service, github, omx_runner = make_service(tmp_path)
    pr_event = NormalizedEvent(
        kind="pull_request_opened",
        repo_full_name="acme/demo",
        action="opened",
        number=77,
        actor_login="agent",
        payload={},
        body=f"Implements #5\n{build_signature(stage='implementation', job='impl-open', issue=5)}",
        title="Feature/#5",
        base_branch="dev",
        head_branch="feature/#5",
        is_pull_request=True,
    )
    service.handle_event(pr_event)
    service.wait_for_idle()

    review_jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=77)
    assert [job.review_round for job in review_jobs] == [1]

    for round_number in (1, 2, 3):
        review_job = service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=77)[-1]
        review_event = NormalizedEvent(
            kind="pull_request_comment",
            repo_full_name="acme/demo",
            action="created",
            number=77,
            actor_login="agent",
            payload={},
            body=build_signature(
                stage="review_round",
                job=review_job.id,
                pr=77,
                round=round_number,
                issue=5,
            ),
            title="Feature/#5",
            is_pull_request=True,
        )
        result = service.handle_event(review_event)
        service.wait_for_idle()
        assert result["stage"] == "implementation"

        implementation_job = service.storage.find_jobs(
            repo_full_name="acme/demo", stage="implementation", pr_number=77
        )[-1]
        implementation_event = NormalizedEvent(
            kind="pull_request_comment",
            repo_full_name="acme/demo",
            action="created",
            number=77,
            actor_login="agent",
            payload={},
            body=build_signature(
                stage="implementation",
                job=implementation_job.id,
                issue=5,
                pr=77,
            ),
            title="Feature/#5",
            is_pull_request=True,
        )
        result = service.handle_event(implementation_event)
        service.wait_for_idle()
        expected_stage = "final_verdict" if round_number == 3 else "review_round"
        assert result["stage"] == expected_stage

    verdict_jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="final_verdict", pr_number=77)
    assert verdict_jobs
    assert omx_runner.launches[-1]["job"].stage == "final_verdict"
    assert [
        job.review_round
        for job in service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=77)
    ] == [1, 2, 3]
    assert len(service.storage.find_jobs(repo_full_name="acme/demo", stage="implementation", pr_number=77)) == 3

    verdict_event = NormalizedEvent(
        kind="pull_request_comment",
        repo_full_name="acme/demo",
        action="created",
        number=77,
        actor_login="agent",
        payload={},
        body=build_signature(stage="final_verdict", job="verdict-1", pr=77, verdict="APPROVE"),
        title="Feature/#5",
        is_pull_request=True,
    )
    service.handle_event(verdict_event)

    assert github.merged == [("acme/demo", 77)]


def test_review_round_verification_requires_exact_signature(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)
    repo = service.storage.get_repo("acme/demo")
    assert repo is not None
    job = JobRecord(repo_full_name=repo.full_name, stage="review_round", pr_number=77, review_round=2)
    expected_signature = build_signature(stage="review_round", job=job.id, pr=77, round=2)
    github.add_pr_signature("acme/demo", 77, build_signature(stage="review_round", job="stale-job", pr=77, round=1))
    github.add_pr_signature("acme/demo", 77, expected_signature)

    service._verify_side_effect(repo, job)


def test_review_round_verification_rejects_stale_signed_comment(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)
    repo = service.storage.get_repo("acme/demo")
    assert repo is not None
    job = JobRecord(repo_full_name=repo.full_name, stage="review_round", pr_number=77, review_round=2)
    github.add_pr_signature("acme/demo", 77, build_signature(stage="review_round", job="stale-job", pr=77, round=1))

    with pytest.raises(RuntimeError, match="review-comment-missing"):
        service._verify_side_effect(repo, job)


def test_duplicate_review_round_event_is_ignored(tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)
    event = NormalizedEvent(
        kind="pull_request_comment",
        repo_full_name="acme/demo",
        action="created",
        number=77,
        actor_login="agent",
        payload={},
        body=build_signature(stage="review_round", job="job-1", pr=77, round=1),
        title="Feature/#5",
        is_pull_request=True,
    )

    first = service.handle_event(event)
    service.wait_for_idle()
    second = service.handle_event(event)
    service.wait_for_idle()

    implementation_jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="implementation", pr_number=77)
    assert first["status"] == "queued"
    assert second == {"status": "ignored", "reason": "duplicate_agent_event"}
    assert len(implementation_jobs) == 1
    assert omx_runner.launches[-1]["job"].stage == "implementation"
    assert omx_runner.launches[-1]["job"].pr_number == 77


def test_implementation_followup_verification_requires_exact_signature(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)
    repo = service.storage.get_repo("acme/demo")
    assert repo is not None
    job = JobRecord(repo_full_name=repo.full_name, stage="implementation", issue_number=5, pr_number=77)
    expected_signature = build_signature(stage="implementation", job=job.id, issue=5, pr=77)
    github.add_pr_signature("acme/demo", 77, build_signature(stage="implementation", job="stale-job", issue=5, pr=77))
    github.add_pr_signature("acme/demo", 77, expected_signature)

    service._verify_side_effect(repo, job)


def test_implementation_followup_verification_rejects_stale_signature(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)
    repo = service.storage.get_repo("acme/demo")
    assert repo is not None
    job = JobRecord(repo_full_name=repo.full_name, stage="implementation", issue_number=5, pr_number=77)
    github.add_pr_signature("acme/demo", 77, build_signature(stage="implementation", job="stale-job", issue=5, pr=77))

    with pytest.raises(RuntimeError, match="implementation-comment-missing"):
        service._verify_side_effect(repo, job)


def test_final_verdict_verification_requires_exact_signature(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)
    repo = service.storage.get_repo("acme/demo")
    assert repo is not None
    job = JobRecord(repo_full_name=repo.full_name, stage="final_verdict", pr_number=77)
    approve_signature = build_signature(stage="final_verdict", job=job.id, pr=77, verdict="APPROVE")
    github.add_pr_signature("acme/demo", 77, build_signature(stage="review_round", job="review-job", pr=77, round=3))
    github.add_pr_signature("acme/demo", 77, approve_signature)

    service._verify_side_effect(repo, job)


def test_final_verdict_verification_rejects_unrelated_signed_comment(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)
    repo = service.storage.get_repo("acme/demo")
    assert repo is not None
    job = JobRecord(repo_full_name=repo.full_name, stage="final_verdict", pr_number=77)
    github.add_pr_signature("acme/demo", 77, build_signature(stage="review_round", job="review-job", pr=77, round=3))

    with pytest.raises(RuntimeError, match="final-verdict-comment-missing"):
        service._verify_side_effect(repo, job)


def test_bootstrap_repo_queues_existing_open_issues(tmp_path: Path) -> None:
    service, github, omx_runner = make_service(tmp_path)
    github.open_issues["acme/demo"] = [
        {"number": 5, "title": "Bootstrap me", "body": "Need sync"},
        {"number": 6, "title": "Skip PR", "body": "PR body", "pull_request": {"url": "x"}},
    ]

    count = service.bootstrap_repo("acme/demo")
    service.wait_for_idle()

    assert count == 1
    assert len(omx_runner.launches) == 1
    first_job = omx_runner.launches[0]["job"]
    assert isinstance(first_job, JobRecord)
    assert first_job.issue_number == 5
    assert first_job.stage == "issue_request"


def test_bootstrap_repo_skips_issues_with_existing_issue_request_signature(tmp_path: Path) -> None:
    service, github, omx_runner = make_service(tmp_path)
    github.open_issues["acme/demo"] = [
        {"number": 5, "title": "Already handled", "body": "Need sync"},
        {"number": 6, "title": "Needs bootstrap", "body": "Need report"},
    ]
    github.add_issue_signature(
        "acme/demo",
        5,
        build_signature(stage="issue_request", job="existing-job", issue=5),
    )

    count = service.bootstrap_repo("acme/demo")
    service.wait_for_idle()

    assert count == 1
    assert len(omx_runner.launches) == 1
    only_job = omx_runner.launches[0]["job"]
    assert isinstance(only_job, JobRecord)
    assert only_job.issue_number == 6
    assert only_job.stage == "issue_request"


def test_pull_request_opened_to_main_is_ignored(tmp_path: Path) -> None:
    service, _, _ = make_service(tmp_path)

    result = service.handle_event(
        NormalizedEvent(
            kind="pull_request_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=13,
            actor_login="human",
            payload={},
            body="release",
            title="Release PR",
            base_branch="main",
            head_branch="release",
            is_pull_request=True,
        )
    )

    assert result == {"status": "ignored", "reason": "release_loop_excluded"}


def test_main_push_queues_dev_sync(tmp_path: Path) -> None:
    dev_syncer = FakeGitDevSyncer()
    service, _, _ = make_service(tmp_path, dev_syncer=dev_syncer)

    result = service.handle_event(
        NormalizedEvent(
            kind="branch_push",
            repo_full_name="acme/demo",
            action="push",
            number=0,
            actor_login="human",
            payload={},
            ref="refs/heads/main",
            commit_sha="abc123",
        )
    )
    service.wait_for_idle()

    assert result["stage"] == "dev_sync"
    assert dev_syncer.sync_calls == [("acme/demo", "abc123")]
    assert service.storage.find_jobs(repo_full_name="acme/demo", stage="dev_sync")[0].status == "completed"


def test_non_main_push_is_ignored(tmp_path: Path) -> None:
    dev_syncer = FakeGitDevSyncer()
    service, _, _ = make_service(tmp_path, dev_syncer=dev_syncer)

    result = service.handle_event(
        NormalizedEvent(
            kind="branch_push",
            repo_full_name="acme/demo",
            action="push",
            number=0,
            actor_login="human",
            payload={},
            ref="refs/heads/dev",
            commit_sha="abc123",
        )
    )

    assert result == {"status": "ignored", "reason": "non_main_push"}
    assert dev_syncer.sync_calls == []


def test_duplicate_main_push_is_ignored(tmp_path: Path) -> None:
    dev_syncer = FakeGitDevSyncer()
    service, _, _ = make_service(tmp_path, dev_syncer=dev_syncer)
    event = NormalizedEvent(
        kind="branch_push",
        repo_full_name="acme/demo",
        action="push",
        number=0,
        actor_login="human",
        payload={},
        ref="refs/heads/main",
        commit_sha="abc123",
    )

    first = service.handle_event(event)
    service.wait_for_idle()
    second = service.handle_event(event)
    service.wait_for_idle()

    assert first["stage"] == "dev_sync"
    assert second == {"status": "ignored", "reason": "duplicate_dev_sync"}
    assert dev_syncer.sync_calls == [("acme/demo", "abc123")]


def test_dev_sync_conflict_launches_omx_and_cleans_up(tmp_path: Path) -> None:
    dev_syncer = FakeGitDevSyncer(conflict=True)
    service, _, omx_runner = make_service(tmp_path, dev_syncer=dev_syncer)

    result = service.handle_event(
        NormalizedEvent(
            kind="branch_push",
            repo_full_name="acme/demo",
            action="push",
            number=0,
            actor_login="human",
            payload={},
            ref="refs/heads/main",
            commit_sha="abc123",
        )
    )
    service.wait_for_idle()

    jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="dev_sync")
    assert result["stage"] == "dev_sync"
    assert omx_runner.launches[-1]["job"].stage == "dev_sync"
    assert len(dev_syncer.verify_calls) == 1
    assert len(dev_syncer.cleanup_calls) == 1
    assert jobs[0].status == "completed"
