from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dani.models import JobRecord
from dani.omo_http_runner import OmoHttpRunner

LIVE_FLAG = "DANI_OMO_LIVE"
LIVE_FLAG_VALUES = {"1", "true", "yes", "on"}
pytestmark = pytest.mark.skipif(
    os.environ.get(LIVE_FLAG, "").strip().lower() not in LIVE_FLAG_VALUES or shutil.which("opencode") is None,
    reason=(
        f"Live opencode tests are opt-in: set {LIVE_FLAG}=1 and install the `opencode` binary. "
        "These tests talk to a real opencode HTTP server and consume model tokens."
    ),
)


@pytest.fixture
def live_repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_path, check=True)  # noqa: S607
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "init"],  # noqa: S607
        cwd=repo_path,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "dani-live",
            "GIT_AUTHOR_EMAIL": "dani-live@example.com",
            "GIT_COMMITTER_NAME": "dani-live",
            "GIT_COMMITTER_EMAIL": "dani-live@example.com",
        },
    )
    return repo_path


def test_live_opencode_http_runner_launch_completes_through_server(tmp_path: Path, live_repo: Path) -> None:
    runner = OmoHttpRunner(run_dir=tmp_path / "runs")
    job = JobRecord(repo_full_name="live/demo", stage="implementation", issue_number=10)

    prompt = "Reply with exactly the words 'http live ok' and stop immediately. Do not use tools."
    session = runner.launch(live_repo, job, prompt)

    try:
        runner.wait(session.runtime_handle, timeout_seconds=240)
    finally:
        runner.close_session(session.runtime_handle)
        runner.shutdown()

    assert session.codex_session_id is not None and session.codex_session_id.startswith("ses_"), (
        f"expected captured opencode session id from HTTP runner, got {session.codex_session_id!r}"
    )
    prompt_text = Path(session.prompt_path).read_text(encoding="utf-8")
    assert prompt_text.startswith("ultrawork\n\n"), f"expected ultrawork-prefixed prompt, got {prompt_text[:60]!r}"

    events_text = Path(session.stdout_path).read_text(encoding="utf-8", errors="replace") if session.stdout_path else ""
    assert any(
        json.loads(line).get("type") == "session.idle"
        for line in events_text.splitlines()
        if line.strip().startswith("{")
    ), f"expected at least one session.idle event in HTTP event log; tail: {events_text[-400:]!r}"


def test_live_opencode_http_runner_resume_continues_prior_session(tmp_path: Path, live_repo: Path) -> None:
    runner = OmoHttpRunner(run_dir=tmp_path / "runs")
    launch_job = JobRecord(repo_full_name="live/demo", stage="implementation", issue_number=11)

    launch_prompt = (
        "Remember this exact keyword for later: AMBER_FALCON_4291. Reply only with the word 'ok' and stop immediately."
    )
    launch_session = runner.launch(live_repo, launch_job, launch_prompt)
    try:
        runner.wait(launch_session.runtime_handle, timeout_seconds=240)
    finally:
        runner.close_session(launch_session.runtime_handle)

    first_id = launch_session.codex_session_id
    assert first_id is not None and first_id.startswith("ses_")

    resume_job = JobRecord(repo_full_name="live/demo", stage="implementation", issue_number=11)
    resume_prompt = (
        "What was the keyword I told you earlier in this conversation? Reply with only the keyword and nothing else."
    )
    resume_session = runner.resume(live_repo, resume_job, resume_prompt, first_id)
    try:
        runner.wait(resume_session.runtime_handle, timeout_seconds=240)
    finally:
        runner.close_session(resume_session.runtime_handle)
        runner.shutdown()

    assert resume_session.codex_session_id == first_id

    events_text = (
        Path(resume_session.stdout_path).read_text(encoding="utf-8", errors="replace")
        if resume_session.stdout_path
        else ""
    )
    assert "AMBER_FALCON_4291" in events_text, (
        f"resume did not continue prior session — keyword missing from event log; tail: {events_text[-400:]!r}"
    )
