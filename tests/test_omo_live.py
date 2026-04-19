from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dani.models import JobRecord
from dani.omo_http_runner import OmoHttpRunner
from dani.omo_runner import OmoRunner

LIVE_FLAG = "DANI_OMO_LIVE"
LIVE_FLAG_VALUES = {"1", "true", "yes", "on"}
pytestmark = pytest.mark.skipif(
    os.environ.get(LIVE_FLAG, "").strip().lower() not in LIVE_FLAG_VALUES or shutil.which("opencode") is None,
    reason=(
        f"Live opencode tests are opt-in: set {LIVE_FLAG}=1 and install the `opencode` binary. "
        "These tests spawn real opencode subprocesses and consume model tokens."
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


def _import_job_record():
    from dani.models import JobRecord

    return JobRecord


def test_live_opencode_launch_emits_session_id_and_completes(tmp_path: Path, live_repo: Path) -> None:
    runner = OmoRunner(run_dir=tmp_path / "runs")
    job_cls = _import_job_record()
    job = job_cls(repo_full_name="live/demo", stage="issue_request", issue_number=1)

    prompt = "Reply with exactly the words 'dani live ok' and stop immediately. Do not use tools."
    session = runner.launch(live_repo, job, prompt)

    try:
        runner.wait(session.runtime_handle, timeout_seconds=240)
    finally:
        runner.close_session(session.runtime_handle)

    assert session.stdout_path is not None and session.stderr_path is not None
    stdout_text = Path(session.stdout_path).read_text(encoding="utf-8", errors="replace")
    stderr_text = Path(session.stderr_path).read_text(encoding="utf-8", errors="replace")

    session_ids = [
        json.loads(line).get("sessionID")
        for line in stdout_text.splitlines()
        if line.strip().startswith("{") and '"sessionID"' in line
    ]
    session_ids = [sid for sid in session_ids if isinstance(sid, str) and sid.startswith("ses_")]
    assert session_ids, f"opencode stdout never contained a sessionID event; stderr preview: {stderr_text[:400]!r}"

    assert session.omx_session_id is not None, (
        "launch() should have captured the session id during its pre-wait poll; "
        f"stdout bytes={len(stdout_text)}, stderr bytes={len(stderr_text)}"
    )
    assert session.omx_session_id == session_ids[0]


def test_live_opencode_resume_continues_prior_session(tmp_path: Path, live_repo: Path) -> None:
    runner = OmoRunner(run_dir=tmp_path / "runs")
    job_cls = _import_job_record()
    launch_job = job_cls(repo_full_name="live/demo", stage="issue_request", issue_number=2)

    launch_prompt = (
        "Remember this exact keyword for later: CRIMSON_MOOSE_7361. Reply only with the word 'ok' and stop immediately."
    )
    launch_session = runner.launch(live_repo, launch_job, launch_prompt)
    try:
        runner.wait(launch_session.runtime_handle, timeout_seconds=240)
    finally:
        runner.close_session(launch_session.runtime_handle)

    first_id = launch_session.omx_session_id
    if first_id is None:
        first_id = runner.get_session_id(launch_session.runtime_handle)
    assert first_id is not None and first_id.startswith("ses_"), (
        f"expected captured session id after launch, got {first_id!r}"
    )

    resume_job = job_cls(repo_full_name="live/demo", stage="issue_followup", issue_number=2)
    resume_prompt = (
        "What was the keyword I told you earlier in this conversation? Reply with only the keyword and nothing else."
    )
    resume_session = runner.resume(live_repo, resume_job, resume_prompt, first_id)

    try:
        runner.wait(resume_session.runtime_handle, timeout_seconds=240)
    finally:
        runner.close_session(resume_session.runtime_handle)

    resume_script_text = Path(resume_session.script_path).read_text(encoding="utf-8")
    assert f"opencode run --session {first_id} --format json --dangerously-skip-permissions" in resume_script_text

    assert resume_session.stdout_path is not None
    resume_stdout = Path(resume_session.stdout_path).read_text(encoding="utf-8", errors="replace")
    assert "CRIMSON_MOOSE_7361" in resume_stdout, (
        "resume did not continue prior session — keyword missing from opencode reply; "
        f"stdout tail: {resume_stdout[-400:]!r}"
    )


def test_live_opencode_ultrawork_prefix_accepted(tmp_path: Path, live_repo: Path) -> None:
    runner = OmoRunner(run_dir=tmp_path / "runs")
    job_cls = _import_job_record()
    job = job_cls(repo_full_name="live/demo", stage="issue_request", issue_number=3)

    prompt = "Reply with exactly the words 'ulw live ok' and stop immediately. Do not use tools. Do not enter any loop."
    session = runner.launch(live_repo, job, prompt)

    prompt_text = Path(session.prompt_path).read_text(encoding="utf-8")
    assert prompt_text.startswith("ultrawork\n\n"), f"expected ultrawork-prefixed prompt, got {prompt_text[:60]!r}"

    try:
        runner.wait(session.runtime_handle, timeout_seconds=240)
    finally:
        runner.close_session(session.runtime_handle)

    assert session.stdout_path is not None and session.stderr_path is not None
    stdout_text = Path(session.stdout_path).read_text(encoding="utf-8", errors="replace")
    stderr_text = Path(session.stderr_path).read_text(encoding="utf-8", errors="replace")
    assert "ses_" in stdout_text, f"opencode stdout missing sessionID events; stderr preview: {stderr_text[:400]!r}"


def test_live_opencode_http_runner_launch_completes_through_server(tmp_path: Path, live_repo: Path) -> None:
    runner = OmoHttpRunner(run_dir=tmp_path / "runs")
    job = JobRecord(repo_full_name="live/demo", stage="issue_request", issue_number=10)

    prompt = "Reply with exactly the words 'http live ok' and stop immediately. Do not use tools."
    session = runner.launch(live_repo, job, prompt)

    try:
        runner.wait(session.runtime_handle, timeout_seconds=240)
    finally:
        runner.close_session(session.runtime_handle)
        runner.shutdown()

    assert session.omx_session_id is not None and session.omx_session_id.startswith("ses_"), (
        f"expected captured opencode session id from HTTP runner, got {session.omx_session_id!r}"
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
    launch_job = JobRecord(repo_full_name="live/demo", stage="issue_request", issue_number=11)

    launch_prompt = (
        "Remember this exact keyword for later: AMBER_FALCON_4291. Reply only with the word 'ok' and stop immediately."
    )
    launch_session = runner.launch(live_repo, launch_job, launch_prompt)
    try:
        runner.wait(launch_session.runtime_handle, timeout_seconds=240)
    finally:
        runner.close_session(launch_session.runtime_handle)

    first_id = launch_session.omx_session_id
    assert first_id is not None and first_id.startswith("ses_")

    resume_job = JobRecord(repo_full_name="live/demo", stage="issue_followup", issue_number=11)
    resume_prompt = (
        "What was the keyword I told you earlier in this conversation? Reply with only the keyword and nothing else."
    )
    resume_session = runner.resume(live_repo, resume_job, resume_prompt, first_id)
    try:
        runner.wait(resume_session.runtime_handle, timeout_seconds=240)
    finally:
        runner.close_session(resume_session.runtime_handle)
        runner.shutdown()

    assert resume_session.omx_session_id == first_id

    events_text = (
        Path(resume_session.stdout_path).read_text(encoding="utf-8", errors="replace")
        if resume_session.stdout_path
        else ""
    )
    assert "AMBER_FALCON_4291" in events_text, (
        f"resume did not continue prior session — keyword missing from event log; tail: {events_text[-400:]!r}"
    )
