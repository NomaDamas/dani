from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from dani.codex_runner import CodexRunner
from dani.errors import RolloutMissingError
from dani.signatures import build_signature


class _ActiveProcess:
    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0

    def kill(self) -> None:
        return None


class _ExitedProcess:
    def poll(self) -> int:
        return 1

    def terminate(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 1

    def kill(self) -> None:
        return None


def test_capture_codex_session_id_matches_exec_signature_and_repo_path(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    sessions_root = tmp_path / "sessions"
    session_day_dir = sessions_root / "2026" / "03" / "19"
    session_day_dir.mkdir(parents=True)
    signature = build_signature(stage="issue_request", job="job-123", issue=7)
    session_file = session_day_dir / "rollout-2026-03-19T11-26-54-session-123.jsonl"
    session_file.write_text(
        "\n".join([
            json.dumps({
                "timestamp": "2026-03-19T02:26:54.703Z",
                "type": "session_meta",
                "payload": {
                    "id": "session-123",
                    "cwd": str(repo_path),
                    "originator": "codex_exec",
                },
            }),
            json.dumps({
                "timestamp": "2026-03-19T02:26:56.936Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"Prompt with {signature}"}],
                },
            }),
        ]),
        encoding="utf-8",
    )
    started_at = time.time() - 1
    runner = CodexRunner(run_dir=tmp_path / "runs", sessions_root=sessions_root)

    codex_session_id = runner._capture_codex_session_id(
        repo_path=repo_path,
        prompt=f"Please use this signature: {signature}",
        started_at=started_at,
        poll_interval=0.01,
        timeout_seconds=0.05,
    )

    assert codex_session_id == "session-123"


def test_close_session_terminates_active_process(tmp_path: Path) -> None:
    runner = CodexRunner(run_dir=tmp_path / "runs")
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    stdout_file = stdout_path.open("w", encoding="utf-8")
    stderr_file = stderr_path.open("w", encoding="utf-8")

    process = _ActiveProcess()
    runner._processes["runtime-123"] = (process, stdout_file, stderr_file)

    runner.close_session("runtime-123")

    assert stdout_file.closed
    assert stderr_file.closed
    assert runner._processes == {}


def test_close_session_skips_missing_process(tmp_path: Path) -> None:
    runner = CodexRunner(run_dir=tmp_path / "runs")
    runner.close_session("runtime-123")


def test_build_script_uses_codex_exec_with_full_permission(tmp_path: Path) -> None:
    runner = CodexRunner(run_dir=tmp_path / "runs")
    script = runner._build_script(repo_path=tmp_path / "repo", prompt_path=tmp_path / "prompt.txt")

    assert "codex exec --dangerously-bypass-approvals-and-sandbox" in script
    assert f"{'o'}{'mx'} exec" not in script


def test_build_resume_script_uses_codex_exec_resume_with_full_permission(tmp_path: Path) -> None:
    runner = CodexRunner(run_dir=tmp_path / "runs")
    script = runner._build_resume_script(
        repo_path=tmp_path / "repo",
        prompt_path=tmp_path / "prompt.txt",
        codex_session_id="session-123",
    )

    assert "codex exec resume --dangerously-bypass-approvals-and-sandbox session-123" in script
    assert f"{'o'}{'mx'} exec" not in script


def test_wait_raises_rollout_missing_error_when_resume_stderr_mentions_no_rollout_found(tmp_path: Path) -> None:
    runner = CodexRunner(run_dir=tmp_path / "runs")
    runtime_handle = "runtime-123"
    runtime_dir = runner.run_dir / runtime_handle
    runtime_dir.mkdir(parents=True)
    stdout_path = tmp_path / "stdout.log"
    stderr_path = runtime_dir / "stderr.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text(
        "Error: thread/resume failed: no rollout found for thread id 019d6829",
        encoding="utf-8",
    )
    stdout_file = stdout_path.open("w", encoding="utf-8")
    stderr_file = stderr_path.open("a", encoding="utf-8")
    process = _ExitedProcess()
    runner._processes[runtime_handle] = (process, stdout_file, stderr_file)

    try:
        with pytest.raises(RolloutMissingError, match="no rollout found"):
            runner.wait(runtime_handle)
    finally:
        stdout_file.close()
        stderr_file.close()


def test_codex_can_resume_uuid_like_session_id(tmp_path: Path) -> None:
    runner = CodexRunner(run_dir=tmp_path / "runs")
    assert runner.can_resume("019da16a-565d-7c81-98c9-4b7ff38a3f9b") is True


def test_codex_can_resume_rejects_gajae_prefixed_session_id(tmp_path: Path) -> None:
    runner = CodexRunner(run_dir=tmp_path / "runs")
    assert runner.can_resume("gjc-25afdf9c7ffekN3dovMQw6meL2") is False


def test_codex_can_resume_rejects_empty_or_none_session_id(tmp_path: Path) -> None:
    runner = CodexRunner(run_dir=tmp_path / "runs")
    assert runner.can_resume("") is False
