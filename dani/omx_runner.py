from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from dani.models import JobRecord, SessionRecord


class OmxRunner:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def launch(self, repo_path: Path, job: JobRecord, prompt: str) -> SessionRecord:
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
        )

    def _build_script(self, *, repo_path: Path, prompt_path: Path) -> str:
        quoted_repo = shlex.quote(str(repo_path))
        quoted_prompt = shlex.quote(str(prompt_path))
        return f'#!/bin/sh\nset -eu\ncd {quoted_repo}\nexec omx --madmax "$(cat {quoted_prompt})"\n'

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
