from pathlib import Path
from typing import cast

from dani.github import GitHubCLI
from dani.models import DaniConfig, JobRecord, NormalizedEvent
from dani.omx_runner import OmxRunner
from dani.service import DaniService
from dani.signatures import build_signature
from dani.storage import JsonStorage
from tests.helpers import FakeGitHubCLI, FakeOmxRunner

TEST_SECRET = "unit-test-secret"


def make_service(tmp_path: Path) -> tuple[DaniService, FakeGitHubCLI, FakeOmxRunner]:
    config = DaniConfig(data_dir=tmp_path / ".dani", webhook_secret=TEST_SECRET)
    storage = JsonStorage(config)
    github = FakeGitHubCLI()
    omx_runner = FakeOmxRunner(github)
    service = DaniService(
        config, storage=storage, github=cast(GitHubCLI, github), omx_runner=cast(OmxRunner, omx_runner)
    )
    service.register_repo("acme/demo", str(tmp_path))
    return service, github, omx_runner


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


def test_review_chain_reaches_verdict_and_merges_on_approve(tmp_path: Path) -> None:
    service, github, omx_runner = make_service(tmp_path)

    for round_number in (1, 2, 3):
        event = NormalizedEvent(
            kind="pull_request_comment",
            repo_full_name="acme/demo",
            action="created",
            number=77,
            actor_login="agent",
            payload={},
            body=build_signature(stage="review_round", job=f"job-{round_number}", pr=77, round=round_number),
            title="Feature/#5",
            is_pull_request=True,
        )
        service.handle_event(event)
        service.wait_for_idle()

    verdict_jobs = service.storage.find_jobs(repo_full_name="acme/demo", stage="final_verdict", pr_number=77)
    assert verdict_jobs
    assert omx_runner.launches[-1]["job"].stage == "final_verdict"

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
