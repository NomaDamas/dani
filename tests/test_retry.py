from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from dani.errors import (
    ClaudeUsageLimitError,
    TransientCapacityError,
    check_claude_usage_limit_error,
    check_transient_capacity_error,
)
from dani.github import GitHubCLI
from dani.models import DaniConfig, NormalizedEvent
from dani.omx_runner import OmxRunner
from dani.service import RETRY_BACKOFF_SECONDS, DaniService
from dani.storage import JsonStorage
from tests.helpers import FakeGitDevSyncer, FakeGitHubCLI, FakeOmxRunner

_CAPACITY_MSG = "capacity"

TEST_SECRET = "unit-test-secret"


def make_service(
    tmp_path: Path, *, dev_syncer: FakeGitDevSyncer | None = None
) -> tuple[DaniService, FakeGitHubCLI, FakeOmxRunner]:
    config = DaniConfig(data_dir=tmp_path / ".dani", webhook_secret=TEST_SECRET)
    storage = JsonStorage(config)
    github = FakeGitHubCLI()
    omx_runner = FakeOmxRunner(github)
    service = DaniService(
        config,
        storage=storage,
        github=cast(GitHubCLI, github),
        omx_runner=cast(OmxRunner, omx_runner),
        dev_syncer=dev_syncer or FakeGitDevSyncer(),
    )
    service.register_repo("acme/demo", str(tmp_path))
    return service, github, omx_runner


def _issue_event(number: int = 11) -> NormalizedEvent:
    return NormalizedEvent(
        kind="issue_opened",
        repo_full_name="acme/demo",
        action="opened",
        number=number,
        actor_login="human",
        payload={},
        body="Need automation",
        title="Need automation",
    )


# --- errors.py unit tests ---


class TestCheckTransientCapacityError:
    def test_detects_capacity_message(self) -> None:
        with pytest.raises(TransientCapacityError, match="Selected model is at capacity"):
            check_transient_capacity_error("ERROR: Selected model is at capacity. Please try a different model.")

    def test_detects_overloaded_message(self) -> None:
        with pytest.raises(TransientCapacityError):
            check_transient_capacity_error("model is currently overloaded")

    def test_ignores_generic_rate_limit(self) -> None:
        # Generic "rate limit exceeded" should NOT be classified as model capacity error
        check_transient_capacity_error("rate limit exceeded")

    def test_ignores_normal_stderr(self) -> None:
        check_transient_capacity_error("some normal log output\nno errors here")

    def test_ignores_empty_string(self) -> None:
        check_transient_capacity_error("")


class TestCheckClaudeUsageLimitError:
    def test_detects_session_window_limit(self) -> None:
        with pytest.raises(ClaudeUsageLimitError) as exc_info:
            check_claude_usage_limit_error("Claude usage limit reached. Your limit will reset at 3 PM.")

        assert exc_info.value.limit_type == "session_window"
        assert exc_info.value.reset_hint == "3 PM"
        assert exc_info.value.suggested_retry_at is not None

    def test_detects_weekly_limit(self) -> None:
        with pytest.raises(ClaudeUsageLimitError) as exc_info:
            check_claude_usage_limit_error("Opus weekly limit reached. It resets on Monday morning.")

        assert exc_info.value.limit_type == "weekly"
        assert exc_info.value.reset_hint == "Monday morning"
        assert exc_info.value.suggested_retry_at is not None

    def test_ignores_generic_rate_limit_language(self) -> None:
        check_claude_usage_limit_error("rate limit exceeded")


# --- service.py retry integration tests ---


@patch("dani.service.time.sleep")
def test_retry_succeeds_after_transient_failure(mock_sleep: object, tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)
    omx_runner.set_transient_failures(1)

    service.handle_event(_issue_event())
    service.wait_for_idle()

    job = service.storage.list_jobs()[0]
    assert job.status == "completed"
    assert job.metadata["retry_attempts"] == 1
    assert len(job.metadata["retry_history"]) == 1
    assert job.metadata["retry_history"][0]["reason"] == "transient_capacity"
    assert len(omx_runner.launches) == 2


@patch("dani.service.time.sleep")
def test_retry_exhaustion_marks_job_failed(mock_sleep: object, tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)
    max_attempts = len(RETRY_BACKOFF_SECONDS) + 1
    omx_runner.set_transient_failures(max_attempts)

    service.handle_event(_issue_event())
    service.wait_for_idle()

    job = service.storage.list_jobs()[0]
    assert job.status == "failed"
    assert job.metadata["retry_exhausted"] is True
    assert job.metadata["retry_attempts"] == max_attempts
    assert len(job.metadata["retry_history"]) == max_attempts
    assert "retry_exhausted" in job.metadata["error"]


@patch("dani.service.time.sleep")
def test_non_transient_error_not_retried(mock_sleep: object, tmp_path: Path) -> None:
    service, github, omx_runner = make_service(tmp_path)

    original_launch = omx_runner.launch

    def failing_launch(*args, **kwargs):
        result = original_launch(*args, **kwargs)
        # Remove the side effect so verify fails with a non-transient error
        github.issue_comment_map.clear()
        return result

    omx_runner.launch = failing_launch  # type: ignore[assignment]

    service.handle_event(_issue_event())
    service.wait_for_idle()

    job = service.storage.list_jobs()[0]
    assert job.status == "failed"
    assert job.metadata.get("retry_exhausted") is not True
    assert len(job.metadata.get("retry_history", [])) == 0


@patch("dani.service.time.sleep")
def test_retry_backoff_delays_are_correct(mock_sleep: object, tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)
    omx_runner.set_transient_failures(2)

    service.handle_event(_issue_event())
    service.wait_for_idle()

    job = service.storage.list_jobs()[0]
    assert job.status == "completed"
    assert mock_sleep.call_count == 2  # type: ignore[union-attr]
    calls = [call.args[0] for call in mock_sleep.call_args_list]  # type: ignore[union-attr]
    assert calls == RETRY_BACKOFF_SECONDS[:2]


@patch("dani.service.time.sleep")
def test_retrying_status_visible_during_retry(mock_sleep: object, tmp_path: Path) -> None:
    service, _, omx_runner = make_service(tmp_path)
    observed_statuses: list[str] = []
    original_sleep = mock_sleep

    def capture_status(seconds: float) -> None:
        jobs = service.storage.list_jobs()
        if jobs:
            observed_statuses.append(jobs[0].status)

    original_sleep.side_effect = capture_status  # type: ignore[union-attr]
    omx_runner.set_transient_failures(1)

    service.handle_event(_issue_event())
    service.wait_for_idle()

    assert "retrying" in observed_statuses


@patch("dani.service.time.sleep")
def test_transient_error_with_side_effect_already_posted_completes(mock_sleep: object, tmp_path: Path) -> None:
    """If the OMX run posted the GitHub comment before the capacity error, don't retry — mark completed."""
    service, _, omx_runner = make_service(tmp_path)

    # Don't use set_transient_failures — we want launch() to post the side effect
    # normally, then have wait() raise a transient error afterward.
    def wait_that_always_fails(runtime_handle: str, **kwargs: object) -> None:
        raise TransientCapacityError(_CAPACITY_MSG, _CAPACITY_MSG)

    omx_runner.wait = wait_that_always_fails  # type: ignore[assignment]

    service.handle_event(_issue_event())
    service.wait_for_idle()

    job = service.storage.list_jobs()[0]
    assert job.status == "completed"
    assert job.metadata.get("note") == "side_effect_already_posted"
    # Should NOT have retried — only 1 launch
    assert len(omx_runner.launches) == 1


@patch("dani.service.time.sleep")
def test_restart_issue_request_with_stale_signed_comments_does_not_false_complete_on_transient(
    mock_sleep: object, tmp_path: Path
) -> None:
    service, github, _ = make_service(tmp_path)
    stale_signature = "<!-- dani:stage=issue_request;job=stale-job;issue=11 -->"
    github.add_issue_signature("acme/demo", 11, stale_signature)

    original_launch = service.omx_runner.launch

    def launch_without_new_side_effect(repo_path: Path, job, prompt: str):
        session = original_launch(repo_path, job, prompt)
        github.issue_comment_map[("acme/demo", 11)] = [{"body": stale_signature}]
        return session

    service.omx_runner.launch = launch_without_new_side_effect  # type: ignore[assignment]

    def wait_that_always_fails(runtime_handle: str, **kwargs: object) -> None:
        raise TransientCapacityError(_CAPACITY_MSG, _CAPACITY_MSG)

    service.omx_runner.wait = wait_that_always_fails  # type: ignore[assignment]

    service.handle_event(_issue_event())
    service.wait_for_idle()

    job = service.storage.list_jobs()[0]
    assert job.status == "failed"
    assert job.metadata.get("note") != "side_effect_already_posted"
