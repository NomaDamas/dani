from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from dani.models import JobRecord, SessionRecord


class OmxRunner:
    def __init__(self, run_dir: Path, sessions_root: Path | None = None) -> None:
        self.run_dir = run_dir
        self.sessions_root = sessions_root or (Path.home() / ".codex" / "sessions")
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def launch(self, repo_path: Path, job: JobRecord, prompt: str) -> SessionRecord:
        started_at = time.time()
        session_token = uuid4().hex[:10]
        tmux_session = f"dani-{job.stage}-{session_token}"
        session_dir = self.run_dir / tmux_session
        session_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = session_dir / "prompt.txt"
        script_path = session_dir / "run.sh"
        prompt_path.write_text(prompt, encoding="utf-8")
        script_path.write_text(self._build_script(repo_path=repo_path, prompt_path=prompt_path), encoding="utf-8")
        script_path.chmod(0o755)
        subprocess.run(["tmux", "new-session", "-d", "-s", tmux_session, str(script_path)], check=True)  # noqa: S603
        pane_id = self._tmux_pane_id(tmux_session)
        omx_session_id = None
        if job.stage == "issue_request":
            omx_session_id = self._capture_omx_session_id(repo_path=repo_path, prompt=prompt, started_at=started_at)
        return SessionRecord(
            repo_full_name=job.repo_full_name,
            stage=job.stage,
            tmux_session=tmux_session,
            pane_id=pane_id,
            prompt_path=str(prompt_path),
            script_path=str(script_path),
            worktree_path=str(repo_path),
            job_id=job.id,
            issue_number=job.issue_number,
            pr_number=job.pr_number,
            review_round=job.review_round,
            omx_session_id=omx_session_id,
        )

    def resume(self, repo_path: Path, job: JobRecord, prompt: str, omx_session_id: str) -> SessionRecord:
        session_token = uuid4().hex[:10]
        tmux_session = f"dani-{job.stage}-{session_token}"
        session_dir = self.run_dir / tmux_session
        session_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = session_dir / "prompt.txt"
        script_path = session_dir / "run.sh"
        prompt_path.write_text(prompt, encoding="utf-8")
        script_path.write_text(
            self._build_resume_script(repo_path=repo_path, prompt_path=prompt_path, omx_session_id=omx_session_id),
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        subprocess.run(["tmux", "new-session", "-d", "-s", tmux_session, str(script_path)], check=True)  # noqa: S603
        pane_id = self._tmux_pane_id(tmux_session)
        return SessionRecord(
            repo_full_name=job.repo_full_name,
            stage=job.stage,
            tmux_session=tmux_session,
            pane_id=pane_id,
            prompt_path=str(prompt_path),
            script_path=str(script_path),
            worktree_path=str(repo_path),
            job_id=job.id,
            issue_number=job.issue_number,
            pr_number=job.pr_number,
            review_round=job.review_round,
            omx_session_id=omx_session_id,
        )

    def _build_script(self, *, repo_path: Path, prompt_path: Path) -> str:
        quoted_repo = shlex.quote(str(repo_path))
        quoted_prompt = shlex.quote(str(prompt_path))
        return f'#!/bin/sh\nset -eu\ncd {quoted_repo}\nexec omx --madmax "$(cat {quoted_prompt})"\n'

    def _build_resume_script(self, *, repo_path: Path, prompt_path: Path, omx_session_id: str) -> str:
        quoted_repo = shlex.quote(str(repo_path))
        quoted_prompt = shlex.quote(str(prompt_path))
        quoted_session_id = shlex.quote(omx_session_id)
        return (
            "#!/bin/sh\n"
            "set -eu\n"
            f"cd {quoted_repo}\n"
            f'exec omx resume {quoted_session_id} "$(cat {quoted_prompt})"\n'
        )

    def _tmux_pane_id(self, tmux_session: str) -> str:
        completed = subprocess.run(  # noqa: S603
            ["tmux", "list-panes", "-t", tmux_session, "-F", "#{pane_id}"],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip().splitlines()[0]

    def wait(self, tmux_session: str, *, poll_interval: float = 0.5, timeout_seconds: float = 1800) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            completed = subprocess.run(["tmux", "has-session", "-t", tmux_session], capture_output=True, text=True)  # noqa: S603
            if completed.returncode != 0:
                return
            time.sleep(poll_interval)
        msg = f"tmux session did not exit before timeout: {tmux_session}"
        raise TimeoutError(msg)

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
            for session_file in sorted(self.sessions_root.rglob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True):
                if session_file.stat().st_mtime < started_at - 1:
                    continue
                payload = self._session_meta_payload(session_file)
                if payload is None:
                    continue
                if payload.get("cwd") != repo_path_str or payload.get("originator") != "codex-tui":
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
