from __future__ import annotations

import json
import time
from pathlib import Path

from dani.omx_runner import OmxRunner
from dani.signatures import build_signature


def test_capture_omx_session_id_matches_exec_signature_and_repo_path(tmp_path: Path) -> None:
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
    runner = OmxRunner(run_dir=tmp_path / "runs", sessions_root=sessions_root)

    omx_session_id = runner._capture_omx_session_id(
        repo_path=repo_path,
        prompt=f"Please use this signature: {signature}",
        started_at=started_at,
        poll_interval=0.01,
        timeout_seconds=0.05,
    )

    assert omx_session_id == "session-123"


def test_close_session_terminates_active_process(tmp_path: Path) -> None:
    runner = OmxRunner(run_dir=tmp_path / "runs")
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    stdout_file = stdout_path.open("w", encoding="utf-8")
    stderr_file = stderr_path.open("w", encoding="utf-8")

    process = type(
        "Process",
        (),
        {
            "poll": lambda self: None,
            "terminate": lambda self: None,
            "wait": lambda self, timeout=None: 0,
            "kill": lambda self: None,
        },
    )()
    runner._processes["runtime-123"] = (process, stdout_file, stderr_file)

    runner.close_session("runtime-123")

    assert stdout_file.closed
    assert stderr_file.closed
    assert runner._processes == {}


def test_close_session_skips_missing_process(tmp_path: Path) -> None:
    runner = OmxRunner(run_dir=tmp_path / "runs")
    runner.close_session("runtime-123")


def test_build_script_uses_omx_exec(tmp_path: Path) -> None:
    runner = OmxRunner(run_dir=tmp_path / "runs")
    script = runner._build_script(repo_path=tmp_path / "repo", prompt_path=tmp_path / "prompt.txt")

    assert "omx exec --dangerously-bypass-approvals-and-sandbox" in script


def test_build_resume_script_uses_omx_exec_resume(tmp_path: Path) -> None:
    runner = OmxRunner(run_dir=tmp_path / "runs")
    script = runner._build_resume_script(
        repo_path=tmp_path / "repo",
        prompt_path=tmp_path / "prompt.txt",
        omx_session_id="session-123",
    )

    assert "omx exec resume session-123 --dangerously-bypass-approvals-and-sandbox" in script
