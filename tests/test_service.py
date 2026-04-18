import threading
from pathlib import Path
from typing import cast

import pytest

from dani.errors import RolloutMissingError
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


def test_issue_request_verification_requires_exact_signature(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)
    repo = service.storage.get_repo("acme/demo")
    assert repo is not None
    job = JobRecord(repo_full_name=repo.full_name, stage="issue_request", issue_number=21)
    expected_signature = build_signature(stage="issue_request", job=job.id, issue=21)
    github.add_issue_signature("acme/demo", 21, build_signature(stage="issue_request", job="stale-job", issue=21))
    github.add_issue_signature("acme/demo", 21, expected_signature)

    service._verify_side_effect(repo, job)


def test_issue_request_verification_rejects_stale_signature(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)
    repo = service.storage.get_repo("acme/demo")
    assert repo is not None
    job = JobRecord(repo_full_name=repo.full_name, stage="issue_request", issue_number=21)
    github.add_issue_signature("acme/demo", 21, build_signature(stage="issue_request", job="stale-job", issue=21))

    with pytest.raises(RuntimeError, match="issue-request-comment-missing"):
        service._verify_side_effect(repo, job)


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


def test_issue_followup_verification_requires_exact_signature(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)
    repo = service.storage.get_repo("acme/demo")
    assert repo is not None
    job = JobRecord(repo_full_name=repo.full_name, stage="issue_followup", issue_number=31)
    expected_signature = build_signature(stage="issue_followup", job=job.id, issue=31)
    github.add_issue_signature("acme/demo", 31, build_signature(stage="issue_followup", job="stale-job", issue=31))
    github.add_issue_signature("acme/demo", 31, expected_signature)

    service._verify_side_effect(repo, job)


def test_issue_followup_verification_rejects_stale_signature(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)
    repo = service.storage.get_repo("acme/demo")
    assert repo is not None
    job = JobRecord(repo_full_name=repo.full_name, stage="issue_followup", issue_number=31)
    github.add_issue_signature("acme/demo", 31, build_signature(stage="issue_followup", job="stale-job", issue=31))

    with pytest.raises(RuntimeError, match="issue-followup-comment-missing"):
        service._verify_side_effect(repo, job)


def test_issue_followup_rollout_missing_marks_job_failed_and_posts_restart_warning(tmp_path: Path) -> None:
    service, github, omx_runner = make_service(tmp_path)
    service.handle_event(
        NormalizedEvent(
            kind="issue_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=33,
            actor_login="human",
            payload={},
            body="Need automation",
            title="Need automation",
        )
    )
    service.wait_for_idle()

    omx_runner.set_resume_failure(
        RolloutMissingError(
            "thread/resume failed: no rollout found for thread id 019d6829",
            "no rollout found",
        )
    )

    service.handle_event(
        NormalizedEvent(
            kind="issue_comment",
            repo_full_name="acme/demo",
            action="created",
            number=33,
            actor_login="human",
            payload={"issue": {"body": "Need automation"}},
            body="Please continue.",
            title="Need automation",
        )
    )
    service.wait_for_idle()

    job = service.storage.find_jobs(repo_full_name="acme/demo", stage="issue_followup", issue_number=33)[0]
    assert job.status == "failed"
    assert job.metadata["error"] == "rollout_missing"
    assert "thread/resume failed" in job.metadata["error_detail"]

    warning_signature = build_signature(stage="session_lost", issue=33)
    warning_comments = github.find_comments_by_signature(
        "acme/demo", 33, kind="issue", signature_fragment=warning_signature
    )
    assert len(warning_comments) == 1
    assert "dani restart-issue acme/demo 33" in warning_comments[0]["body"]
    latest_comment = github.latest_signature_comment("acme/demo", 33, kind="issue")
    assert latest_comment is not None
    assert latest_comment[1]["stage"] == "session_lost"
    assert latest_comment[1]["issue"] == "33"


def test_issue_followup_rollout_missing_warning_comment_is_posted_only_once(tmp_path: Path) -> None:
    service, github, omx_runner = make_service(tmp_path)
    service.handle_event(
        NormalizedEvent(
            kind="issue_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=34,
            actor_login="human",
            payload={},
            body="Need automation",
            title="Need automation",
        )
    )
    service.wait_for_idle()
    omx_runner.set_resume_failure(
        RolloutMissingError(
            "thread/resume failed: no rollout found for thread id 019d6829",
            "no rollout found",
        )
    )

    event = NormalizedEvent(
        kind="issue_comment",
        repo_full_name="acme/demo",
        action="created",
        number=34,
        actor_login="human",
        payload={"issue": {"body": "Need automation"}},
        body="Please continue.",
        title="Need automation",
    )

    service.handle_event(event)
    service.wait_for_idle()
    service.handle_event(event)
    service.wait_for_idle()

    warning_comments = github.find_comments_by_signature(
        "acme/demo",
        34,
        kind="issue",
        signature_fragment=build_signature(stage="session_lost", issue=34),
    )
    assert len(warning_comments) == 1
    matching_keys = [
        key
        for key in service.storage.snapshot()["processed_events"]["keys"]
        if "stage=session_lost" in key and "issue=34" in key
    ]
    assert len(matching_keys) == 1
    failed_jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="issue_followup", issue_number=34)
    assert len(failed_jobs) == 2
    assert all(job.status == "failed" for job in failed_jobs)


def test_issue_followup_rollout_missing_retries_warning_after_comment_post_failure(tmp_path: Path) -> None:
    service, github, omx_runner = make_service(tmp_path)
    service.handle_event(
        NormalizedEvent(
            kind="issue_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=35,
            actor_login="human",
            payload={},
            body="Need automation",
            title="Need automation",
        )
    )
    service.wait_for_idle()
    omx_runner.set_resume_failure(
        RolloutMissingError(
            "thread/resume failed: no rollout found for thread id 019d6829",
            "no rollout found",
        )
    )

    original_create_issue_comment = github.create_issue_comment
    attempts = 0

    def flaky_create_issue_comment(repo_full_name: str, issue_number: int, body: str) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            msg = "temporary GitHub failure"
            raise RuntimeError(msg)
        return original_create_issue_comment(repo_full_name, issue_number, body)

    github.create_issue_comment = flaky_create_issue_comment  # type: ignore[assignment]

    event = NormalizedEvent(
        kind="issue_comment",
        repo_full_name="acme/demo",
        action="created",
        number=35,
        actor_login="human",
        payload={"issue": {"body": "Need automation"}},
        body="Please continue.",
        title="Need automation",
    )

    service.handle_event(event)
    service.wait_for_idle()
    service.handle_event(event)
    service.wait_for_idle()

    warning_comments = github.find_comments_by_signature(
        "acme/demo",
        35,
        kind="issue",
        signature_fragment=build_signature(stage="session_lost", issue=35),
    )
    assert attempts == 2
    assert len(warning_comments) == 1
    matching_keys = [
        key
        for key in service.storage.snapshot()["processed_events"]["keys"]
        if "stage=session_lost" in key and "issue=35" in key
    ]
    assert len(matching_keys) == 1


def test_restart_issue_supersedes_existing_jobs_and_enqueues_new_issue_request(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)
    service.queue_manager.submit = lambda job: None  # type: ignore[assignment]
    stale_request = JobRecord(
        repo_full_name="acme/demo",
        stage="issue_request",
        issue_number=41,
        status="completed",
        metadata={"title": "Restart me", "body": "Original body"},
    )
    stale_followup = JobRecord(
        repo_full_name="acme/demo",
        stage="issue_followup",
        issue_number=41,
        status="failed",
        metadata={"omx_session_id": "omx-stale", "title": "Restart me", "body": "Original body"},
    )
    untouched = JobRecord(repo_full_name="acme/demo", stage="issue_request", issue_number=99, status="completed")
    service.storage.create_job(stale_request)
    service.storage.create_job(stale_followup)
    service.storage.create_job(untouched)
    github.issue_comment_map[("acme/demo", 41)] = [
        {"body": "Earlier user context", "user": {"login": "human"}},
        {"body": "Earlier dani reply", "user": {"login": "dani"}},
    ]

    new_job = service.restart_issue("acme/demo", 41)

    refreshed_request = service.storage.get_job(stale_request.id)
    refreshed_followup = service.storage.get_job(stale_followup.id)
    untouched_job = service.storage.get_job(untouched.id)
    assert refreshed_request is not None and refreshed_request.status == "superseded"
    assert refreshed_followup is not None and refreshed_followup.status == "superseded"
    assert untouched_job is not None and untouched_job.status == "completed"
    assert new_job.stage == "issue_request"
    assert new_job.status == "queued"
    assert new_job.issue_number == 41
    repo = service.storage.get_repo("acme/demo")
    assert repo is not None
    prompt = service._build_prompt(repo, new_job)
    assert "Earlier user context" in prompt
    assert "Earlier dani reply" in prompt


def test_issue_comment_with_ignore_signature_is_ignored_before_followup(tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)
    service.handle_event(
        NormalizedEvent(
            kind="issue_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=36,
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
            number=36,
            actor_login="human",
            payload={"issue": {"body": "Need automation"}},
            body="Please ignore this.\n<!-- dani:stage=ignore -->",
            title="Need automation",
        )
    )
    service.wait_for_idle()

    assert result == {"status": "ignored", "reason": "comment_opt_out"}
    assert service.storage.find_jobs(repo_full_name="acme/demo", stage="issue_followup", issue_number=36) == []
    assert omx_runner.resumes == []


def test_issue_comment_with_ignore_command_overrides_approve(tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)

    result = service.handle_event(
        NormalizedEvent(
            kind="issue_comment",
            repo_full_name="acme/demo",
            action="created",
            number=37,
            actor_login="human",
            payload={"issue": {"body": "context"}},
            body="/approve\n/dani ignore",
            title="Need automation",
        )
    )
    service.wait_for_idle()

    assert result == {"status": "ignored", "reason": "comment_opt_out"}
    assert service.storage.find_jobs(repo_full_name="acme/demo", stage="implementation", issue_number=37) == []
    assert omx_runner.launches == []


def test_approve_comment_queues_implementation(tmp_path: Path) -> None:
    service, github, omx_runner = make_service(tmp_path)
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
    assert github.issue_labels[("acme/demo", 11)] == ["Implementing"]
    assert omx_runner.launches[0]["job"].stage == "implementation"
    assert service.storage.list_jobs()[0].status == "completed"


def test_repeated_approve_keeps_implementing_label_idempotent(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)
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

    service.handle_event(event)
    service.wait_for_idle()
    service.handle_event(event)
    service.wait_for_idle()

    assert github.issue_labels[("acme/demo", 11)] == ["Implementing"]


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


def test_external_pr_from_account_younger_than_one_year_is_closed(tmp_path: Path) -> None:
    service, github, omx_runner = make_service(tmp_path)
    github.users["newcomer"] = {"login": "newcomer", "created_at": "3000-01-01T00:00:00Z"}

    result = service.handle_event(
        make_pr_event(pr_number=92, action="opened", body="Implements #21", actor_login="newcomer")
    )
    service.wait_for_idle()

    assert result == {"status": "closed", "reason": "contributor_account_too_new", "pr_number": 92}
    assert github.closed_pull_requests == [("acme/demo", 92)]
    assert service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=92) == []
    assert omx_runner.launches == []
    comments = github.pr_comments("acme/demo", 92)
    assert len(comments) == 1
    assert "at least one year old" in comments[0]["body"]
    assert "open an issue instead" in comments[0]["body"]


def test_external_pr_uses_pull_request_author_created_at_instead_of_sender(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)
    github.users["maintainer"] = {"login": "maintainer", "created_at": "2000-01-01T00:00:00Z"}
    event = make_pr_event(pr_number=93, action="reopened", body="Implements #21", actor_login="maintainer")
    event.payload["pull_request"]["user"] = {
        "login": "newcomer",
        "created_at": "3000-01-01T00:00:00Z",
    }

    result = service.handle_event(event)
    service.wait_for_idle()

    assert result == {"status": "closed", "reason": "contributor_account_too_new", "pr_number": 93}
    assert github.closed_pull_requests == [("acme/demo", 93)]


def test_external_pr_from_account_at_least_one_year_old_queues_review_round(tmp_path: Path) -> None:
    service, github, omx_runner = make_service(tmp_path)
    github.users["veteran"] = {"login": "veteran", "created_at": "2000-01-01T00:00:00Z"}

    result = service.handle_event(
        make_pr_event(pr_number=94, action="opened", body="Implements #21", actor_login="veteran")
    )
    service.wait_for_idle()

    assert result["stage"] == "review_round"
    assert github.closed_pull_requests == []
    assert all("at least one year old" not in comment["body"] for comment in github.pr_comments("acme/demo", 94))
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


def test_external_review_comment_queues_human_escalation_on_tenth_completed_pass(tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)

    for round_number in range(1, 11):
        action = "opened" if round_number == 1 else "synchronize"
        result = service.handle_event(
            make_pr_event(pr_number=88, action=action, body="Implements #21", commit_sha=f"sha-{round_number}")
        )
        service.wait_for_idle()

        assert result["stage"] == "review_round"
        review_job = service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=88)[-1]
        comment_result = service.handle_event(
            make_pr_comment_event(
                pr_number=88,
                body=build_signature(stage="review_round", job=review_job.id, pr=88, round=round_number),
            )
        )
        service.wait_for_idle()

        if round_number < 10:
            assert comment_result == {"status": "updated", "stage": "review_round"}
            assert service.storage.find_jobs(repo_full_name="acme/demo", stage="human_escalation", pr_number=88) == []
            continue

        assert comment_result["stage"] == "human_escalation"

    review_jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=88)
    assert [job.review_round for job in review_jobs] == list(range(1, 11))
    assert all(job.status == "completed" for job in review_jobs)

    escalation_jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="human_escalation", pr_number=88)
    assert len(escalation_jobs) == 1
    assert omx_runner.launches[-1]["job"].stage == "human_escalation"

    post_limit = service.handle_event(
        make_pr_event(
            pr_number=88,
            action="synchronize",
            body="Implements #21",
            commit_sha="sha-11",
        )
    )
    service.wait_for_idle()

    assert post_limit == {"status": "ignored", "reason": "human_review_required"}
    assert service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=88) == review_jobs
    assert len(service.storage.find_jobs(repo_full_name="acme/demo", stage="human_escalation", pr_number=88)) == 1


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


def test_approve_verdict_with_merge_conflict_queues_resolution_job(tmp_path: Path) -> None:
    service, github, omx_runner = make_service(tmp_path)
    github.merge_conflicts.add(("acme/demo", 77))
    github.add_pull_request(
        "acme/demo",
        77,
        "Implements #5\n<!-- dani:stage=implementation;job=impl-1;issue=5 -->",
        title="Feature/#5",
        head_branch="Feature/#5",
        base_branch="dev",
    )

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

    result = service.handle_event(verdict_event)
    service.wait_for_idle()

    resolution_jobs = service.storage.find_jobs(
        repo_full_name="acme/demo", stage="merge_conflict_resolution", pr_number=77
    )
    assert result["stage"] == "merge_conflict_resolution"
    assert resolution_jobs
    assert resolution_jobs[0].issue_number == 5
    assert resolution_jobs[0].metadata["head_branch"] == "Feature/#5"
    assert omx_runner.launches[-1]["job"].stage == "merge_conflict_resolution"
    assert github.merged == []


def test_approve_verdict_with_merge_conflict_reuses_tracked_issue_number_without_pr_body_reference(
    tmp_path: Path,
) -> None:
    service, github, _ = make_service(tmp_path)
    github.merge_conflicts.add(("acme/demo", 77))
    service.storage.create_job(
        JobRecord(
            repo_full_name="acme/demo",
            stage="review_round",
            issue_number=5,
            pr_number=77,
            review_round=1,
            status="completed",
        )
    )
    github.add_pull_request(
        "acme/demo",
        77,
        "No issue reference in body",
        title="Feature without issue in body",
        head_branch="feature/no-issue",
        base_branch="dev",
    )

    result = service.handle_event(
        NormalizedEvent(
            kind="pull_request_comment",
            repo_full_name="acme/demo",
            action="created",
            number=77,
            actor_login="agent",
            payload={},
            body=build_signature(stage="final_verdict", job="verdict-1", pr=77, verdict="APPROVE"),
            title="Feature without issue in body",
            is_pull_request=True,
        )
    )
    service.wait_for_idle()

    resolution_jobs = service.storage.find_jobs(
        repo_full_name="acme/demo", stage="merge_conflict_resolution", pr_number=77
    )
    assert result["stage"] == "merge_conflict_resolution"
    assert resolution_jobs[0].issue_number == 5
    assert resolution_jobs[0].metadata["head_branch"] == "feature/no-issue"


def test_merge_conflict_resolution_comment_queues_final_verdict_retry(tmp_path: Path) -> None:
    service, github, omx_runner = make_service(tmp_path)
    pr_body = "Implements #5\n<!-- dani:stage=implementation;job=impl-1;issue=5 -->"
    github.add_pull_request(
        "acme/demo",
        77,
        pr_body,
        title="Feature/#5",
        head_branch="Feature/#5",
        base_branch="dev",
    )

    resolution_event = NormalizedEvent(
        kind="pull_request_comment",
        repo_full_name="acme/demo",
        action="created",
        number=77,
        actor_login="agent",
        payload={},
        body=build_signature(stage="merge_conflict_resolution", job="resolve-1", pr=77),
        title="Feature/#5",
        is_pull_request=True,
    )

    result = service.handle_event(resolution_event)
    service.wait_for_idle()

    verdict_jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="final_verdict", pr_number=77)
    assert result["stage"] == "final_verdict"
    assert verdict_jobs
    assert verdict_jobs[0].issue_number == 5
    assert verdict_jobs[0].metadata["title"] == "Feature/#5"
    assert verdict_jobs[0].metadata["body"] == pr_body
    assert omx_runner.launches[-1]["job"].stage == "final_verdict"


def test_duplicate_merge_conflict_resolution_event_is_ignored(tmp_path: Path) -> None:
    service, github, omx_runner = make_service(tmp_path)
    github.add_pull_request(
        "acme/demo",
        77,
        "Implements #5\n<!-- dani:stage=implementation;job=impl-1;issue=5 -->",
        title="Feature/#5",
        head_branch="Feature/#5",
        base_branch="dev",
    )
    event = NormalizedEvent(
        kind="pull_request_comment",
        repo_full_name="acme/demo",
        action="created",
        number=77,
        actor_login="agent",
        payload={},
        body=build_signature(stage="merge_conflict_resolution", job="resolve-1", pr=77),
        title="Feature/#5",
        is_pull_request=True,
    )

    first = service.handle_event(event)
    service.wait_for_idle()
    second = service.handle_event(event)
    service.wait_for_idle()

    verdict_jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="final_verdict", pr_number=77)
    assert first["status"] == "queued"
    assert second == {"status": "ignored", "reason": "duplicate_agent_event"}
    assert len(verdict_jobs) == 1
    assert omx_runner.launches[-1]["job"].stage == "final_verdict"
    assert omx_runner.launches[-1]["job"].pr_number == 77


def test_merge_conflict_resolution_requires_its_own_signed_comment(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)
    repo = service.storage.get_repo("acme/demo")
    assert repo is not None
    job = JobRecord(repo_full_name=repo.full_name, stage="merge_conflict_resolution", pr_number=77)
    github.add_pr_signature(
        "acme/demo",
        77,
        build_signature(stage="final_verdict", job="verdict-1", pr=77, verdict="APPROVE"),
    )

    with pytest.raises(RuntimeError, match="merge-conflict-comment-missing"):
        service._verify_side_effect(repo, job)


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
        body=build_signature(stage="review_round", job="job-1", pr=77, round=1, issue=5),
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


def test_duplicate_final_verdict_event_is_ignored(tmp_path: Path) -> None:
    service, github, omx_runner = make_service(tmp_path)
    github.merge_conflicts.add(("acme/demo", 77))
    github.add_pull_request(
        "acme/demo",
        77,
        "Implements #5\n<!-- dani:stage=implementation;job=impl-1;issue=5 -->",
        title="Feature/#5",
        head_branch="Feature/#5",
        base_branch="dev",
    )
    event = NormalizedEvent(
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

    first = service.handle_event(event)
    service.wait_for_idle()
    second = service.handle_event(event)
    service.wait_for_idle()

    resolution_jobs = service.storage.find_jobs(
        repo_full_name="acme/demo", stage="merge_conflict_resolution", pr_number=77
    )
    assert first["status"] == "queued"
    assert second == {"status": "ignored", "reason": "duplicate_agent_event"}
    assert len(resolution_jobs) == 1
    assert omx_runner.launches[-1]["job"].stage == "merge_conflict_resolution"
    assert omx_runner.launches[-1]["job"].pr_number == 77


def test_final_verdict_transient_failure_allows_redelivery(tmp_path: Path) -> None:
    """A transient merge failure must not poison redelivery — the retry must succeed."""
    from github.GithubException import GithubException

    service, github, _omx_runner = make_service(tmp_path)
    service.storage.create_job(
        JobRecord(
            repo_full_name="acme/demo",
            stage="review_round",
            issue_number=5,
            pr_number=77,
            review_round=1,
            status="completed",
        )
    )
    github.add_pull_request(
        "acme/demo",
        77,
        "Implements #5\n<!-- dani:stage=implementation;job=impl-1;issue=5 -->",
        title="Feature/#5",
        head_branch="feature/5",
        base_branch="dev",
    )
    original_merge = github.merge_pull_request

    def boom(repo_full_name: str, pr_number: int) -> None:
        raise GithubException(500, {"message": "GitHub outage"}, {})

    github.merge_pull_request = boom  # type: ignore[assignment]
    event = NormalizedEvent(
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

    with pytest.raises(GithubException):
        service.handle_event(event)

    # Restore normal merge and redeliver the same event
    github.merge_pull_request = original_merge  # type: ignore[assignment]
    result = service.handle_event(event)
    assert result["status"] == "merged"
    assert ("acme/demo", 77) in github.merged


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


def test_review_round_stops_when_pr_is_closed(tmp_path: Path) -> None:
    """Review round agent event is ignored when the PR has been closed."""
    service, github, _ = make_service(tmp_path)
    github.add_pull_request("acme/demo", 77, "Implements #5")

    # Close the PR before the review round event arrives
    github.close_pull_request("acme/demo", 77)

    review_event = NormalizedEvent(
        kind="pull_request_comment",
        repo_full_name="acme/demo",
        action="created",
        number=77,
        actor_login="agent",
        payload={},
        body=build_signature(stage="review_round", job="r-1", pr=77, round=1, issue=5),
        title="Feature/#5",
        is_pull_request=True,
    )
    result = service.handle_event(review_event)
    service.wait_for_idle()

    assert result["status"] == "ignored"
    assert result["reason"] == "pr_not_open"
    assert service.storage.find_jobs(repo_full_name="acme/demo", stage="implementation", pr_number=77) == []


def test_implementation_stops_when_pr_is_closed(tmp_path: Path) -> None:
    """Implementation agent event is ignored when the PR has been closed."""
    service, github, _ = make_service(tmp_path)
    github.add_pull_request("acme/demo", 77, "Implements #5")

    # Close the PR before the implementation event arrives
    github.close_pull_request("acme/demo", 77)

    impl_event = NormalizedEvent(
        kind="pull_request_comment",
        repo_full_name="acme/demo",
        action="created",
        number=77,
        actor_login="agent",
        payload={},
        body=build_signature(stage="implementation", job="impl-1", pr=77, issue=5),
        title="Feature/#5",
        is_pull_request=True,
    )
    result = service.handle_event(impl_event)
    service.wait_for_idle()

    assert result["status"] == "ignored"
    assert result["reason"] == "pr_not_open"
    assert service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=77) == []
    assert service.storage.find_jobs(repo_full_name="acme/demo", stage="final_verdict", pr_number=77) == []


def test_final_verdict_stops_when_pr_is_closed(tmp_path: Path) -> None:
    """Final verdict agent event is ignored when the PR has been closed."""
    service, github, _ = make_service(tmp_path)
    github.add_pull_request("acme/demo", 77, "Implements #5")

    github.close_pull_request("acme/demo", 77)

    verdict_event = NormalizedEvent(
        kind="pull_request_comment",
        repo_full_name="acme/demo",
        action="created",
        number=77,
        actor_login="agent",
        payload={},
        body=build_signature(stage="final_verdict", job="v-1", pr=77, verdict="APPROVE"),
        title="Feature/#5",
        is_pull_request=True,
    )
    result = service.handle_event(verdict_event)

    assert result["status"] == "ignored"
    assert result["reason"] == "pr_not_open"
    assert github.merged == []


def test_pr_opened_without_issue_reference_is_ignored(tmp_path: Path) -> None:
    """A PR opened without any linked issue number is dropped as untracked."""
    service, _, omx_runner = make_service(tmp_path)

    result = service.handle_event(
        NormalizedEvent(
            kind="pull_request_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=42,
            actor_login="external-contributor",
            payload={},
            body="Some changes without issue reference",
            title="External contribution",
            base_branch="dev",
            head_branch="feature/external",
            is_pull_request=True,
        )
    )

    assert result == {"status": "ignored", "reason": "untracked_pr"}
    assert service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=42) == []
    assert omx_runner.launches == []


def test_review_round_without_issue_drops_untracked_pr(tmp_path: Path) -> None:
    """Review-round agent event for a PR with no traceable issue is dropped."""
    service, _, omx_runner = make_service(tmp_path)

    review_event = NormalizedEvent(
        kind="pull_request_comment",
        repo_full_name="acme/demo",
        action="created",
        number=42,
        actor_login="agent",
        payload={},
        body=build_signature(stage="review_round", job="r-1", pr=42, round=1),
        title="External contribution",
        is_pull_request=True,
    )
    result = service.handle_event(review_event)

    assert result == {"status": "ignored", "reason": "untracked_pr"}
    assert service.storage.find_jobs(repo_full_name="acme/demo", stage="implementation", pr_number=42) == []
    assert omx_runner.launches == []


def test_implementation_without_issue_drops_untracked_pr(tmp_path: Path) -> None:
    """Implementation agent event for a PR with no traceable issue is dropped."""
    service, _, omx_runner = make_service(tmp_path)

    impl_event = NormalizedEvent(
        kind="pull_request_comment",
        repo_full_name="acme/demo",
        action="created",
        number=42,
        actor_login="agent",
        payload={},
        body=build_signature(stage="implementation", job="impl-1", pr=42),
        title="External contribution",
        is_pull_request=True,
    )
    result = service.handle_event(impl_event)

    assert result == {"status": "ignored", "reason": "untracked_pr"}
    assert service.storage.find_jobs(repo_full_name="acme/demo", stage="review_round", pr_number=42) == []
    assert service.storage.find_jobs(repo_full_name="acme/demo", stage="final_verdict", pr_number=42) == []
    assert omx_runner.launches == []
