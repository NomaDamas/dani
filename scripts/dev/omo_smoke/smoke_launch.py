from __future__ import annotations

import sys
import time
from pathlib import Path

from dani.agent_runner import build_agent_runner
from dani.models import JobRecord

REPO_PATH = Path("/tmp/dani-omo-smoke/repo")
RUN_DIR = Path("/tmp/dani-omo-smoke/runs-launch")


def ensure_repo() -> None:
    import subprocess

    REPO_PATH.mkdir(parents=True, exist_ok=True)
    if not (REPO_PATH / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=REPO_PATH, check=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-q", "-m", "init"],
            cwd=REPO_PATH,
            check=True,
        )


def main() -> int:
    ensure_repo()
    runner = build_agent_runner("omo", RUN_DIR)
    print(f"[smoke-launch] runner class: {type(runner).__name__}")

    job = JobRecord(repo_full_name="acme/smoke", stage="issue_request", issue_number=42)
    prompt = "Reply with exactly the words 'dani omo smoke ok' and stop. Do not use tools. Do not call any agents."

    t0 = time.monotonic()
    session = runner.launch(REPO_PATH, job, prompt)
    print(f"[smoke-launch] launch() took {time.monotonic() - t0:.2f}s")
    print(f"[smoke-launch] captured session id: {session.omx_session_id!r}")
    script_text = Path(session.script_path).read_text(encoding="utf-8")
    assert "opencode run --format json --dangerously-skip-permissions" in script_text
    assert f"cd {REPO_PATH}" in script_text

    runner.wait(session.runtime_handle, timeout_seconds=180)
    runner.close_session(session.runtime_handle)

    assert session.stdout_path is not None
    stdout_text = Path(session.stdout_path).read_text(encoding="utf-8", errors="replace")
    captured = session.omx_session_id or runner.get_session_id(session.runtime_handle)
    assert captured is not None and captured.startswith("ses_"), f"expected a ses_... session id, got {captured!r}"
    print(f"[smoke-launch] stdout head: {stdout_text[:300]}")
    print("[smoke-launch] ==> PASS: opencode ran; session id captured; script matched expectations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
