from __future__ import annotations

import os
import shlex
import subprocess
import threading
from pathlib import Path
from typing import Final, TextIO
from uuid import uuid4

from dani.agent_runner import ManagedProcess
from dani.errors import check_rollout_missing_error, check_transient_capacity_error
from dani.models import DEFAULT_AGENT_TIMEOUT_SECONDS, JobRecord, SessionRecord

DEFAULT_GAJAE_BIN: Final = "gjc"
DEFAULT_GAJAE_MODEL: Final = "nomadamas/gpt-5.5"
DEFAULT_GAJAE_PLAN_MODEL: Final = "nomadamas-anthropic/opus-4.8"
DEFAULT_GAJAE_FINAL_MODEL: Final = "nomadamas/gpt-5.5"

GAJAE_BIN_ENV: Final = "DANI_GAJAE_BIN"
GAJAE_MODEL_ENV: Final = "DANI_GAJAE_MODEL"
GAJAE_PLAN_MODEL_ENV: Final = "DANI_GAJAE_PLAN_MODEL"
GAJAE_FINAL_MODEL_ENV: Final = "DANI_GAJAE_FINAL_MODEL"

PLANNING_STAGES: Final = frozenset({
    "issue_request",
    "issue_followup",
    "issue_request_recovery",
    "issue_followup_recovery",
})


def gajae_model_for_stage(stage: str) -> str:
    if stage in PLANNING_STAGES:
        return os.environ.get(GAJAE_PLAN_MODEL_ENV, DEFAULT_GAJAE_PLAN_MODEL)
    if stage == "final_verdict":
        return os.environ.get(GAJAE_FINAL_MODEL_ENV, DEFAULT_GAJAE_FINAL_MODEL)
    return os.environ.get(GAJAE_MODEL_ENV, DEFAULT_GAJAE_MODEL)


class GajaeRunner:
    def __init__(self, run_dir: Path, *, gajae_bin: str | None = None) -> None:
        self.run_dir = run_dir
        self.gajae_bin = gajae_bin or os.environ.get(GAJAE_BIN_ENV, DEFAULT_GAJAE_BIN)
        self._processes: dict[str, tuple[ManagedProcess, TextIO, TextIO]] = {}
        self._lock = threading.RLock()
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def launch(self, repo_path: Path, job: JobRecord, prompt: str) -> SessionRecord:
        session_token = uuid4().hex[:10]
        process_handle = f"dani-{job.stage}-{session_token}"
        session_dir = self.run_dir / process_handle
        session_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = session_dir / "prompt.txt"
        script_path = session_dir / "run.sh"
        stdout_path = session_dir / "stdout.log"
        stderr_path = session_dir / "stderr.log"
        prompt_path.write_text(prompt, encoding="utf-8")
        script_path.write_text(
            self._build_script(repo_path=repo_path, prompt_path=prompt_path, stage=job.stage),
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        stdout_file = stdout_path.open("w", encoding="utf-8")
        stderr_file = stderr_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            [str(script_path)],
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )
        with self._lock:
            self._processes[process_handle] = (process, stdout_file, stderr_file)
        return SessionRecord(
            repo_full_name=job.repo_full_name,
            stage=job.stage,
            runtime_handle=process_handle,
            prompt_path=str(prompt_path),
            script_path=str(script_path),
            worktree_path=str(repo_path),
            job_id=job.id,
            issue_number=job.issue_number,
            pr_number=job.pr_number,
            review_round=job.review_round,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            preferred_runtime="gajae",
            effective_runtime="gajae",
            native_session_runtime="gajae",
        )

    def resume(self, repo_path: Path, job: JobRecord, prompt: str, codex_session_id: str) -> SessionRecord:
        del codex_session_id
        return self.launch(repo_path, job, prompt)

    def _build_script(self, *, repo_path: Path, prompt_path: Path, stage: str) -> str:
        quoted_repo = shlex.quote(str(repo_path))
        quoted_prompt = shlex.quote(str(prompt_path))
        quoted_bin = shlex.quote(self.gajae_bin)
        quoted_model = shlex.quote(gajae_model_for_stage(stage))
        quoted_plan_model = shlex.quote(os.environ.get(GAJAE_PLAN_MODEL_ENV, DEFAULT_GAJAE_PLAN_MODEL))
        return (
            "#!/bin/sh\n"
            "set -eu\n"
            f"cd {quoted_repo}\n"
            f'exec {quoted_bin} --print --model {quoted_model} --plan {quoted_plan_model} "$(cat {quoted_prompt})"\n'
        )

    def wait(
        self, runtime_handle: str, *, poll_interval: float = 0.5, timeout_seconds: float = DEFAULT_AGENT_TIMEOUT_SECONDS
    ) -> None:
        del poll_interval
        with self._lock:
            entry = self._processes.get(runtime_handle)
        if entry is None:
            return
        process, _, _ = entry
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            msg = f"gjc headless process did not exit before timeout: {runtime_handle}"
            raise TimeoutError(msg) from exc
        stderr_text = self._stderr_text(runtime_handle)
        check_rollout_missing_error(stderr_text)
        check_transient_capacity_error(stderr_text)
        if return_code != 0:
            msg = stderr_text.strip() or f"gjc headless process exited with status {return_code}"
            raise RuntimeError(msg)

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
        del runtime_handle
        return None

    def can_resume(self, session_id: str) -> bool:
        del session_id
        return False

    def _stderr_text(self, runtime_handle: str) -> str:
        stderr_path = self.run_dir / runtime_handle / "stderr.log"
        if not stderr_path.exists():
            return ""
        return stderr_path.read_text(encoding="utf-8", errors="replace")
