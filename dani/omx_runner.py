from __future__ import annotations

import json
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Protocol, TextIO
from uuid import uuid4

from dani.models import JobRecord, SessionRecord


class ManagedProcess(Protocol):
    def poll(self) -> object: ...
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = None) -> object: ...
    def kill(self) -> None: ...


class OmxRunner:
    def __init__(self, run_dir: Path, sessions_root: Path | None = None) -> None:
        self.run_dir = run_dir
        self.sessions_root = sessions_root or (Path.home() / ".codex" / "sessions")
        self._processes: dict[str, tuple[ManagedProcess, TextIO, TextIO]] = {}
        self._lock = threading.RLock()
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def launch(self, repo_path: Path, job: JobRecord, prompt: str) -> SessionRecord:
        started_at = time.time()
        session_token = uuid4().hex[:10]
        process_handle = f"dani-{job.stage}-{session_token}"
        session_dir = self.run_dir / process_handle
        session_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = session_dir / "prompt.txt"
        script_path = session_dir / "run.sh"
        stdout_path = session_dir / "stdout.log"
        stderr_path = session_dir / "stderr.log"
        prompt_path.write_text(prompt, encoding="utf-8")
        script_path.write_text(self._build_script(repo_path=repo_path, prompt_path=prompt_path), encoding="utf-8")
        script_path.chmod(0o755)
        stdout_file = stdout_path.open("w", encoding="utf-8")
        stderr_file = stderr_path.open("w", encoding="utf-8")
        process = subprocess.Popen([str(script_path)], stdout=stdout_file, stderr=stderr_file)  # noqa: S603
        with self._lock:
            self._processes[process_handle] = (process, stdout_file, stderr_file)
        omx_session_id = None
        if job.stage == "issue_request":
            omx_session_id = self._capture_omx_session_id(repo_path=repo_path, prompt=prompt, started_at=started_at)
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
            omx_session_id=omx_session_id,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )

    def resume(self, repo_path: Path, job: JobRecord, prompt: str, omx_session_id: str) -> SessionRecord:
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
            self._build_resume_script(repo_path=repo_path, prompt_path=prompt_path, omx_session_id=omx_session_id),
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        stdout_file = stdout_path.open("w", encoding="utf-8")
        stderr_file = stderr_path.open("w", encoding="utf-8")
        process = subprocess.Popen([str(script_path)], stdout=stdout_file, stderr=stderr_file)  # noqa: S603
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
            omx_session_id=omx_session_id,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )

    def _build_script(self, *, repo_path: Path, prompt_path: Path) -> str:
        quoted_repo = shlex.quote(str(repo_path))
        quoted_prompt = shlex.quote(str(prompt_path))
        return (
            "#!/bin/sh\n"
            "set -eu\n"
            f"cd {quoted_repo}\n"
            f'exec omx exec --dangerously-bypass-approvals-and-sandbox "$(cat {quoted_prompt})"\n'
        )

    def _build_resume_script(self, *, repo_path: Path, prompt_path: Path, omx_session_id: str) -> str:
        quoted_repo = shlex.quote(str(repo_path))
        quoted_prompt = shlex.quote(str(prompt_path))
        quoted_session_id = shlex.quote(omx_session_id)
        return (
            "#!/bin/sh\n"
            "set -eu\n"
            f"cd {quoted_repo}\n"
            f'exec omx exec resume {quoted_session_id} --dangerously-bypass-approvals-and-sandbox "$(cat {quoted_prompt})"\n'
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
            msg = f"omx exec process did not exit before timeout: {runtime_handle}"
            raise TimeoutError(msg) from exc

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

    def _capture_omx_session_id(
        self,
        *,
        repo_path: Path,
        prompt: str,
        started_at: float,
        poll_interval: float = 1.0,
        timeout_seconds: float = 45.0,
    ) -> str | None:
        signature = self._signature_from_prompt(prompt)
        if signature is None or not self.sessions_root.exists():
            return None

        deadline = time.monotonic() + timeout_seconds
        repo_path_str = str(repo_path)
        while time.monotonic() < deadline:
            for session_file in sorted(
                self.sessions_root.rglob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True
            ):
                if session_file.stat().st_mtime < started_at - 1:
                    continue
                payload = self._session_meta_payload(session_file)
                if payload is None:
                    continue
                if payload.get("cwd") != repo_path_str:
                    continue
                if payload.get("originator") not in {"codex-tui", "codex_exec"}:
                    continue
                text = session_file.read_text(encoding="utf-8")
                if signature in text:
                    return str(payload.get("id") or "") or None
            time.sleep(poll_interval)
        return None

    def _session_meta_payload(self, session_file: Path) -> dict[str, str] | None:
        try:
            first_line = session_file.read_text(encoding="utf-8").splitlines()[0]
        except (FileNotFoundError, IndexError):
            return None
        try:
            record = json.loads(first_line)
        except json.JSONDecodeError:
            return None
        payload = record.get("payload")
        return payload if isinstance(payload, dict) else None

    def _signature_from_prompt(self, prompt: str) -> str | None:
        matches = re.findall(r"<!--\s*dani:[^>]+-->", prompt)
        return matches[-1] if matches else None
