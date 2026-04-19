from __future__ import annotations

import json
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import TextIO
from uuid import uuid4

from dani.agent_runner import ManagedProcess
from dani.errors import (
    check_opencode_session_missing_error,
    check_rollout_missing_error,
    check_transient_capacity_error,
)
from dani.models import JobRecord, SessionRecord

OPENCODE_BIN = "opencode"
ULTRAWORK_PROMPT_PREFIX = "ultrawork\n\n"
PLANNING_STAGES = frozenset({"issue_request", "issue_followup"})


class OmoRunner:
    """Agent runner that drives the ``opencode`` CLI (oh-my-openagents stack)."""

    def __init__(
        self,
        run_dir: Path,
        opencode_bin: str = OPENCODE_BIN,
    ) -> None:
        self.run_dir = run_dir
        self.opencode_bin = opencode_bin
        self._processes: dict[str, tuple[ManagedProcess, TextIO, TextIO]] = {}
        self._lock = threading.RLock()
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def _decorated_prompt(self, prompt: str, *, stage: str | None = None) -> str:
        if prompt.lstrip().lower().startswith("ultrawork"):
            return prompt
        if stage in PLANNING_STAGES:
            return prompt
        return f"{ULTRAWORK_PROMPT_PREFIX}{prompt}"

    def launch(self, repo_path: Path, job: JobRecord, prompt: str) -> SessionRecord:
        session_dir, prompt_path, script_path, stdout_path, stderr_path, handle = self._prepare_session_files(job)
        prompt_path.write_text(self._decorated_prompt(prompt, stage=job.stage), encoding="utf-8")
        script_path.write_text(
            self._build_script(repo_path=repo_path, prompt_path=prompt_path),
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        process, stdout_file, stderr_file = self._spawn(script_path, stdout_path, stderr_path)
        with self._lock:
            self._processes[handle] = (process, stdout_file, stderr_file)
        omx_session_id = None
        if job.stage == "issue_request":
            omx_session_id = self._capture_session_id(stdout_path=stdout_path)
        del session_dir
        return SessionRecord(
            repo_full_name=job.repo_full_name,
            stage=job.stage,
            runtime_handle=handle,
            prompt_path=str(prompt_path),
            script_path=str(script_path),
            worktree_path=str(repo_path),
            job_id=job.id,
            issue_number=job.issue_number,
            pr_number=job.pr_number,
            review_round=job.review_round,
            omx_session_id=omx_session_id,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )

    def resume(self, repo_path: Path, job: JobRecord, prompt: str, omx_session_id: str) -> SessionRecord:
        session_dir, prompt_path, script_path, stdout_path, stderr_path, handle = self._prepare_session_files(job)
        prompt_path.write_text(self._decorated_prompt(prompt, stage=job.stage), encoding="utf-8")
        script_path.write_text(
            self._build_resume_script(
                repo_path=repo_path,
                prompt_path=prompt_path,
                opencode_session_id=omx_session_id,
            ),
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        process, stdout_file, stderr_file = self._spawn(script_path, stdout_path, stderr_path)
        with self._lock:
            self._processes[handle] = (process, stdout_file, stderr_file)
        del session_dir
        return SessionRecord(
            repo_full_name=job.repo_full_name,
            stage=job.stage,
            runtime_handle=handle,
            prompt_path=str(prompt_path),
            script_path=str(script_path),
            worktree_path=str(repo_path),
            job_id=job.id,
            issue_number=job.issue_number,
            pr_number=job.pr_number,
            review_round=job.review_round,
            omx_session_id=omx_session_id,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )

    def wait(self, runtime_handle: str, *, poll_interval: float = 0.5, timeout_seconds: float = 1800) -> None:
        del poll_interval
        with self._lock:
            entry = self._processes.get(runtime_handle)
        if entry is None:
            return
        process, _, _ = entry
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            msg = f"opencode run process did not exit before timeout: {runtime_handle}"
            raise TimeoutError(msg) from exc
        self._check_stderr_for_transient_error(runtime_handle)

    def close_session(self, runtime_handle: str) -> None:
        with self._lock:
            entry = self._processes.pop(runtime_handle, None)
        if entry is None:
            return
        process, stdout_file, stderr_file = entry
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            stdout_file.close()
            stderr_file.close()

    def get_session_id(self, runtime_handle: str) -> str | None:
        stdout_path = self.run_dir / runtime_handle / "stdout.log"
        return self._session_id_from_stdout(stdout_path)

    def can_resume(self, session_id: str) -> bool:
        return bool(session_id) and session_id.startswith("ses_")

    def _prepare_session_files(self, job: JobRecord) -> tuple[Path, Path, Path, Path, Path, str]:
        session_token = uuid4().hex[:10]
        handle = f"dani-{job.stage}-{session_token}"
        session_dir = self.run_dir / handle
        session_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = session_dir / "prompt.txt"
        script_path = session_dir / "run.sh"
        stdout_path = session_dir / "stdout.log"
        stderr_path = session_dir / "stderr.log"
        return session_dir, prompt_path, script_path, stdout_path, stderr_path, handle

    def _spawn(
        self, script_path: Path, stdout_path: Path, stderr_path: Path
    ) -> tuple[subprocess.Popen[bytes], TextIO, TextIO]:
        stdout_file = stdout_path.open("w", encoding="utf-8")
        stderr_file = stderr_path.open("w", encoding="utf-8")
        process = subprocess.Popen(  # noqa: S603
            [str(script_path)],
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )
        return process, stdout_file, stderr_file

    def _build_script(self, *, repo_path: Path, prompt_path: Path) -> str:
        quoted_repo = shlex.quote(str(repo_path))
        quoted_prompt = shlex.quote(str(prompt_path))
        quoted_bin = shlex.quote(self.opencode_bin)
        return (
            "#!/bin/sh\n"
            "set -eu\n"
            f"cd {quoted_repo}\n"
            f'exec {quoted_bin} run --format json --dangerously-skip-permissions "$(cat {quoted_prompt})"\n'
        )

    def _build_resume_script(self, *, repo_path: Path, prompt_path: Path, opencode_session_id: str) -> str:
        quoted_repo = shlex.quote(str(repo_path))
        quoted_prompt = shlex.quote(str(prompt_path))
        quoted_session_id = shlex.quote(opencode_session_id)
        quoted_bin = shlex.quote(self.opencode_bin)
        return (
            "#!/bin/sh\n"
            "set -eu\n"
            f"cd {quoted_repo}\n"
            f"exec {quoted_bin} run --session {quoted_session_id} --format json "
            f'--dangerously-skip-permissions "$(cat {quoted_prompt})"\n'
        )

    def _check_stderr_for_transient_error(self, runtime_handle: str) -> None:
        stderr_path = self.run_dir / runtime_handle / "stderr.log"
        if stderr_path.exists():
            stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
            check_rollout_missing_error(stderr_text)
            check_opencode_session_missing_error(stderr_text)
            check_transient_capacity_error(stderr_text)
        stdout_path = self.run_dir / runtime_handle / "stdout.log"
        if stdout_path.exists():
            stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
            check_opencode_session_missing_error(stdout_text)
            check_transient_capacity_error(stdout_text)

    def _capture_session_id(
        self,
        *,
        stdout_path: Path,
        poll_interval: float = 0.5,
        timeout_seconds: float = 45.0,
    ) -> str | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            session_id = self._session_id_from_stdout(stdout_path)
            if session_id:
                return session_id
            time.sleep(poll_interval)
        return None

    def _session_id_from_stdout(self, stdout_path: Path) -> str | None:
        if not stdout_path.exists():
            return None
        try:
            text = stdout_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        for line in text.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = event.get("sessionID") or event.get("sessionId")
            if isinstance(session_id, str) and session_id.startswith("ses_"):
                return session_id
        return None
