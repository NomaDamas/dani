import threading
from pathlib import Path
from typing import cast

import pytest

from dani.agent_runner import AgentRunner
from dani.errors import ClaudeUsageLimitError, RolloutMissingError
from dani.github import GitHubCLI
from dani.models import RUNTIME_OMO, RUNTIME_OMX, DaniConfig, JobRecord, NormalizedEvent
from dani.omx_runner import OmxRunner
from dani.service import DaniService
from dani.session_bridge import BridgeContext, OmoSessionBridge
from dani.signatures import build_signature
from dani.storage import JsonStorage
from tests.helpers import FakeGitDevSyncer, FakeGitHubCLI, FakeOmxRunner, FakeRuntimeRunner

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
        omx_runner=cast(AgentRunner, omx_runner),
        dev_syncer=dev_syncer or FakeGitDevSyncer(),
    )
    service.register_repo("acme/demo", str(tmp_path))
    return service, github, omx_runner


def make_omo_preferred_service(
    tmp_path: Path,
    *,
    dev_syncer: FakeGitDevSyncer | None = None,
    bridge_context: BridgeContext | None = None,
) -> tuple[DaniService, FakeGitHubCLI, FakeRuntimeRunner, FakeRuntimeRunner]:
    class StubBridge:
        def __init__(self, context: BridgeContext | None) -> None:
            self.context = context
            self.calls: list[tuple[str, str | None]] = []

        def load(self, *, repo_path: Path, session_id: str | None = None) -> BridgeContext | None:
            self.calls.append((str(repo_path), session_id))
            return self.context

    config = DaniConfig(data_dir=tmp_path / ".dani", webhook_secret=TEST_SECRET, agent_runtime=RUNTIME_OMO)
    storage = JsonStorage(config)
    github = FakeGitHubCLI()
    omo_runner = FakeRuntimeRunner(github, runtime_name=RUNTIME_OMO)
    omx_runner = FakeRuntimeRunner(github, runtime_name=RUNTIME_OMX)
    bridge = StubBridge(bridge_context)
    service = DaniService(
        config,
        storage=storage,
        github=cast(GitHubCLI, github),
        omx_runner=cast(OmxRunner, omo_runner),
        dev_syncer=dev_syncer or FakeGitDevSyncer(),
        runtime_runners={RUNTIME_OMX: cast(OmxRunner, omx_runner)},
        session_bridge=cast(OmoSessionBridge, bridge),
    )
    service.register_repo("acme/demo", str(tmp_path))
    return service, github, omo_runner, omx_runner


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


def test_issue_request_falls_back_from_omo_to_omx_on_claude_session_limit(tmp_path: Path) -> None:
    bridge_context = BridgeContext(
        prompt_block="Prior OMO context (imported summary; not a native resume):\n- Open thread: finish edge cases",
        source_session_id="ses_prior_123",
        note="from_test",
    )
    service, _, omo_runner, omx_runner = make_omo_preferred_service(tmp_path, bridge_context=bridge_context)
    omo_runner.queue_wait_error(
        ClaudeUsageLimitError(
            "Claude usage limit reached",
            "Claude usage limit reached",
            "session_window",
            reset_hint="in 5 hours",
            suggested_retry_at="2026-04-22T08:00:00+00:00",
        )
    )

    service.handle_event(
        NormalizedEvent(
            kind="issue_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=51,
            actor_login="human",
            payload={},
            body="Need automation",
            title="Need automation",
        )
    )
    service.wait_for_idle()

    job = service.storage.find_jobs(repo_full_name="acme/demo", stage="issue_request", issue_number=51)[0]
    sessions = service.storage.list_sessions()
    assert job.status == "completed"
    assert job.metadata["preferred_runtime"] == RUNTIME_OMO
    assert job.metadata["effective_runtime"] == RUNTIME_OMX
    assert job.metadata["fallback_reason"] == "claude_session_window_limit"
    assert job.metadata["usage_limit_kind"] == "session_window"
    assert job.metadata["bridge_source_session_id"] == "ses_prior_123"
    assert len(omo_runner.launches) == 1
    assert len(omx_runner.launches) == 1
    assert "Prior OMO context" in omx_runner.launches[0]["prompt"]
    assert "not a native resume" in omx_runner.launches[0]["prompt"]
    assert [session.effective_runtime for session in sessions] == [RUNTIME_OMO, RUNTIME_OMX]
    assert sessions[0].status == "failed"
    assert sessions[1].status == "completed"


def test_issue_request_uses_cached_claude_weekly_limit_to_start_directly_on_omx(tmp_path: Path) -> None:
    service, _, omo_runner, omx_runner = make_omo_preferred_service(tmp_path)
    service.storage.create_job(
        JobRecord(
            repo_full_name="acme/demo",
            stage="issue_request",
            issue_number=1,
            status="failed",
            metadata={
                "usage_limit_runtime": RUNTIME_OMO,
                "usage_limit_kind": "weekly",
                "usage_limit_until": "2099-01-01T00:00:00+00:00",
            },
        )
    )

    service.handle_event(
        NormalizedEvent(
            kind="issue_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=52,
            actor_login="human",
            payload={},
            body="Need automation",
            title="Need automation",
        )
    )
    service.wait_for_idle()

    job = service.storage.find_jobs(repo_full_name="acme/demo", stage="issue_request", issue_number=52)[0]
    assert job.status == "completed"
    assert job.metadata["effective_runtime"] == RUNTIME_OMX
    assert job.metadata["fallback_reason"] == "cached_claude_usage_limit"
    assert omo_runner.launches == []
    assert len(omx_runner.launches) == 1


def test_issue_followup_after_omo_fallback_continues_on_omx_session(tmp_path: Path) -> None:
    service, _, omo_runner, omx_runner = make_omo_preferred_service(tmp_path)
    omo_runner.queue_wait_error(
        ClaudeUsageLimitError(
            "Opus weekly limit reached",
            "weekly limit reached",
            "weekly",
            reset_hint="next week",
            suggested_retry_at="2026-04-29T00:00:00+00:00",
        )
    )

    service.handle_event(
        NormalizedEvent(
            kind="issue_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=53,
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
            number=53,
            actor_login="human",
            payload={"issue": {"body": "Need automation"}},
            body="Please continue on the latest plan.",
            title="Need automation",
        )
    )
    service.wait_for_idle()

    followup_job = service.storage.find_jobs(repo_full_name="acme/demo", stage="issue_followup", issue_number=53)[0]
    assert result["stage"] == "issue_followup"
    assert followup_job.status == "completed"
    assert followup_job.metadata["effective_runtime"] == RUNTIME_OMX
    assert len(omo_runner.resumes) == 0
    assert len(omx_runner.resumes) == 1
    assert omx_runner.resumes[0]["job"].stage == "issue_followup"


def test_service_build_prompt_uses_effective_runtime_not_configured_runtime(tmp_path: Path) -> None:
    service, _, _, _ = make_omo_preferred_service(tmp_path)
    repo = service.storage.get_repo("acme/demo")
    assert repo is not None
    job = JobRecord(repo_full_name="acme/demo", stage="implementation", issue_number=54)

    omx_prompt = service._build_prompt(repo, job, runtime=RUNTIME_OMX)
    omo_prompt = service._build_prompt(repo, job, runtime=RUNTIME_OMO)

    assert "$ralph" in omx_prompt
    assert "$ralph" not in omo_prompt
    assert "ultrawork" in omo_prompt


def test_issue_followup_verification_requires_exact_signature(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)
    repo = service.storage.get_repo("acme/demo")
    assert repo is not None
    job = JobRecord(repo_full_name=repo.full_name, stage="issue_followup", issue_number=31)
    expected_signature = build_signature(stage="issue_followup", job=job.id, issue=31)
    github.add_issue_signature("acme/demo", 31, build_signature(stage="issue_followup", job="stale-job", issue=31))
    github.add_issue_signature("acme/demo", 31, expected_signature)

    service._verify_side_effect(repo, job)


def test_issue_comment_with_unresumable_prior_session_falls_back_to_fresh_issue_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dani.models import SessionRecord

    service, _, omx_runner = make_service(tmp_path)
    repo = service.storage.get_repo("acme/demo")
    assert repo is not None

    legacy_session_id = "019da0f4-6ef1-7923-811f-57eb3e93bd8e"
    legacy_session = SessionRecord(
        repo_full_name=repo.full_name,
        stage="issue_request",
        runtime_handle="runtime-legacy",
        prompt_path=str(tmp_path / "legacy-prompt.txt"),
        script_path=str(tmp_path / "legacy.sh"),
        worktree_path=str(tmp_path),
        job_id="legacy-job-id",
        issue_number=731,
        omx_session_id=legacy_session_id,
    )
    service.storage.create_session(legacy_session)

    monkeypatch.setattr(
        omx_runner,
        "can_resume",
        lambda session_id: bool(session_id) and not session_id.startswith("019d"),
    )

    service.handle_event(
        NormalizedEvent(
            kind="issue_comment",
            repo_full_name="acme/demo",
            action="created",
            number=731,
            actor_login="human",
            payload={"issue": {"body": "fallback comment"}},
            body="this is a fresh comment on a legacy issue",
            title="Legacy followup",
        )
    )
    service.wait_for_idle()

    request_jobs = [
        job for job in service.storage.list_jobs() if job.stage == "issue_request" and job.issue_number == 731
    ]
    followup_jobs = [
        job for job in service.storage.list_jobs() if job.stage == "issue_followup" and job.issue_number == 731
    ]
    assert request_jobs, "expected a fresh issue_request to be enqueued when prior session id is non-resumable"
    assert not followup_jobs, "must NOT enqueue an issue_followup against an un-resumable session id"
    assert not omx_runner.resumes, "runner.resume must not be invoked when can_resume returned False"
    new_job = next(job for job in request_jobs if job.id != legacy_session.job_id)
    assert new_job.metadata["rerouted_from"] == "issue_followup"
    assert new_job.metadata["prior_session_id"] == legacy_session_id
    assert new_job.metadata["comment_body"] == "this is a fresh comment on a legacy issue"
    assert any(launch["job"].issue_number == 731 for launch in omx_runner.launches), (
        "runner.launch must run a fresh session for the legacy issue"
    )


def test_issue_comment_with_resumable_prior_session_still_resumes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dani.models import SessionRecord

    service, _, omx_runner = make_service(tmp_path)
    repo = service.storage.get_repo("acme/demo")
    assert repo is not None

    resumable_session_id = "ses_25ad70836ffemLP2sYPAkGq8hd"
    resumable_session = SessionRecord(
        repo_full_name=repo.full_name,
        stage="issue_request",
        runtime_handle="runtime-resumable",
        prompt_path=str(tmp_path / "prompt.txt"),
        script_path=str(tmp_path / "run.sh"),
        worktree_path=str(tmp_path),
        job_id="resumable-job-id",
        issue_number=732,
        omx_session_id=resumable_session_id,
    )
    service.storage.create_session(resumable_session)

    monkeypatch.setattr(
        omx_runner,
        "can_resume",
        lambda session_id: bool(session_id) and session_id.startswith("ses_"),
    )

    service.handle_event(
        NormalizedEvent(
            kind="issue_comment",
            repo_full_name="acme/demo",
            action="created",
            number=732,
            actor_login="human",
            payload={"issue": {"body": "resume me"}},
            body="continue please",
            title="Resumable followup",
        )
    )
    service.wait_for_idle()

    followup_jobs = [
        job for job in service.storage.list_jobs() if job.stage == "issue_followup" and job.issue_number == 732
    ]
    assert followup_jobs, "expected an issue_followup job when prior session id is resumable"
    assert any(resume["omx_session_id"] == resumable_session_id for resume in omx_runner.resumes), (
        "runner.resume must be invoked with the resumable session id"
    )


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
        omx_runner=cast(AgentRunner, omx_runner),
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


def test_external_pr_to_main_posts_retarget_comment_and_is_ignored(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)

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

    assert result == {"status": "ignored", "reason": "non_dev_target_branch"}
    posted = github.pr_comment_map.get(("acme/demo", 13), [])
    assert len(posted) == 1
    assert "<!-- dani:stage=retarget_request;pr=13 -->" in posted[0]["body"]
    assert "`dev`" in posted[0]["body"]
    assert "`main`" in posted[0]["body"]


def test_external_pr_to_non_dev_feature_branch_posts_retarget_comment(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)

    result = service.handle_event(
        NormalizedEvent(
            kind="pull_request_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=14,
            actor_login="contributor",
            payload={},
            body="some body",
            title="External feature PR",
            base_branch="feature/legacy",
            head_branch="contributor:fix",
            is_pull_request=True,
        )
    )

    assert result == {"status": "ignored", "reason": "non_dev_target_branch"}
    posted = github.pr_comment_map.get(("acme/demo", 14), [])
    assert len(posted) == 1
    assert "`feature/legacy`" in posted[0]["body"]


def test_external_pr_retarget_comment_is_idempotent_across_resyncs(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)
    base_event = NormalizedEvent(
        kind="pull_request_opened",
        repo_full_name="acme/demo",
        action="opened",
        number=15,
        actor_login="contributor",
        payload={},
        body="contribution",
        title="External PR",
        base_branch="main",
        head_branch="contributor:fix",
        is_pull_request=True,
    )

    service.handle_event(base_event)
    sync_event = NormalizedEvent(
        kind="pull_request_opened",
        repo_full_name="acme/demo",
        action="synchronize",
        number=15,
        actor_login="contributor",
        payload={},
        body="contribution",
        title="External PR",
        base_branch="main",
        head_branch="contributor:fix",
        is_pull_request=True,
    )
    service.handle_event(sync_event)
    service.handle_event(sync_event)

    posted = github.pr_comment_map.get(("acme/demo", 15), [])
    assert len(posted) == 1, f"retarget comment must post exactly once across re-fires; got {len(posted)} comments"


def test_agent_managed_pr_to_main_does_not_post_retarget_comment(tmp_path: Path) -> None:
    from dani.signatures import build_signature as _build_sig

    service, github, _ = make_service(tmp_path)
    agent_signature = _build_sig(stage="implementation", job="agent-job", issue=99, pr=16)
    result = service.handle_event(
        NormalizedEvent(
            kind="pull_request_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=16,
            actor_login="dani-bot",
            payload={},
            body=f"Implementation PR\n\n{agent_signature}",
            title="Agent PR to main (defensive)",
            base_branch="main",
            head_branch="feature/#99",
            is_pull_request=True,
        )
    )

    assert result == {"status": "ignored", "reason": "release_loop_excluded"}
    posted = github.pr_comment_map.get(("acme/demo", 16), [])
    assert posted == [], "agent-managed PR to main must not get the retarget comment"


def test_dani_retarget_comment_received_back_is_ignored_no_action(tmp_path: Path) -> None:
    from dani.signatures import build_signature as _build_sig

    service, _, _ = make_service(tmp_path)
    retarget_sig = _build_sig(stage="retarget_request", pr=17)
    result = service.handle_event(
        NormalizedEvent(
            kind="pull_request_comment",
            repo_full_name="acme/demo",
            action="created",
            number=17,
            actor_login="dani-bot",
            payload={},
            body=f"Thanks for the contribution!\n\n{retarget_sig}",
            title="External PR",
            is_pull_request=True,
        )
    )

    assert result == {"status": "ignored", "reason": "retarget_request_no_action"}


def test_release_pr_from_dev_to_main_is_silently_ignored_no_retarget_comment(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)

    result = service.handle_event(
        NormalizedEvent(
            kind="pull_request_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=18,
            actor_login="maintainer",
            payload={},
            body="Release dev into main",
            title="Release PR",
            base_branch="main",
            head_branch="dev",
            is_pull_request=True,
        )
    )

    assert result == {"status": "ignored", "reason": "release_loop_excluded"}
    assert github.pr_comment_map.get(("acme/demo", 18), []) == [], (
        "release PR (dev -> main) must not trigger a retarget comment"
    )


def test_external_fork_pr_to_dev_proceeds_to_review_round(tmp_path: Path) -> None:
    service, github, _ = make_service(tmp_path)

    result = service.handle_event(
        NormalizedEvent(
            kind="pull_request_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=19,
            actor_login="outside-contributor",
            payload={"pull_request": {"head": {"sha": "fork-sha-19"}}},
            body="Fixes a bug in the docs\n\nCloses #42",
            title="Docs fix from fork",
            base_branch="dev",
            head_branch="fix/docs",
            commit_sha="fork-sha-19",
            is_pull_request=True,
        )
    )

    assert result.get("status") == "queued"
    assert result.get("stage") == "review_round"
    assert github.pr_comment_map.get(("acme/demo", 19), []) == [], (
        "external PR already targeting dev must not get a retarget comment"
    )
    jobs = [job for job in service.storage.list_jobs() if job.pr_number == 19]
    assert jobs, "external fork PR to dev should enqueue a review_round job"
    assert jobs[0].metadata.get("external_contribution") is True, (
        "external fork PR must be flagged external_contribution=True in job metadata"
    )


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


def test_dev_sync_conflict_falls_back_from_omo_to_omx_on_weekly_limit(tmp_path: Path) -> None:
    dev_syncer = FakeGitDevSyncer(conflict=True)
    service, _, omo_runner, omx_runner = make_omo_preferred_service(tmp_path, dev_syncer=dev_syncer)
    omo_runner.queue_wait_error(
        ClaudeUsageLimitError(
            "Opus weekly limit reached",
            "weekly limit reached",
            "weekly",
            reset_hint="next week",
            suggested_retry_at="2026-04-29T00:00:00+00:00",
        )
    )

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

    job = service.storage.find_jobs(repo_full_name="acme/demo", stage="dev_sync")[0]
    assert result["stage"] == "dev_sync"
    assert job.status == "completed"
    assert job.metadata["effective_runtime"] == RUNTIME_OMX
    assert job.metadata["usage_limit_kind"] == "weekly"
    assert len(omo_runner.launches) == 1
    assert len(omx_runner.launches) == 1
    assert len(dev_syncer.verify_calls) == 1
    assert len(dev_syncer.cleanup_calls) == 1


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
