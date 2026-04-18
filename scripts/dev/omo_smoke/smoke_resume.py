from __future__ import annotations

import sys
import time
from pathlib import Path

from dani.agent_runner import build_agent_runner
from dani.models import JobRecord

REPO_PATH = Path("/tmp/dani-omo-smoke/repo")
RUN_DIR = Path("/tmp/dani-omo-smoke/runs-resume")


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

    job1 = JobRecord(repo_full_name="acme/smoke", stage="issue_request", issue_number=77)
    launch_prompt = "Remember this exact keyword: PURPLE_HIPPO_9182. Reply only with 'ok' and stop."

    print("[smoke-resume] phase 1: launching opencode session...")
    t0 = time.monotonic()
    session1 = runner.launch(REPO_PATH, job1, launch_prompt)
    runner.wait(session1.runtime_handle, timeout_seconds=180)
    runner.close_session(session1.runtime_handle)
    first_sid = session1.omx_session_id or runner.get_session_id(session1.runtime_handle)
    assert first_sid is not None and first_sid.startswith("ses_")
    print(f"[smoke-resume] phase 1 session id: {first_sid} ({time.monotonic() - t0:.1f}s)")

    job2 = JobRecord(repo_full_name="acme/smoke", stage="issue_followup", issue_number=77)
    resume_prompt = (
        "What was the keyword I told you earlier in this conversation? Reply with only the keyword and nothing else."
    )

    print("[smoke-resume] phase 2: resuming prior session via --session flag...")
    t1 = time.monotonic()
    session2 = runner.resume(REPO_PATH, job2, resume_prompt, first_sid)
    script = Path(session2.script_path).read_text(encoding="utf-8")
    assert f"opencode run --session {first_sid} --format json --dangerously-skip-permissions" in script
    runner.wait(session2.runtime_handle, timeout_seconds=180)
    runner.close_session(session2.runtime_handle)
    stdout_text = Path(session2.stdout_path).read_text(encoding="utf-8", errors="replace")
    print(f"[smoke-resume] phase 2 wall: {time.monotonic() - t1:.1f}s")

    assert "PURPLE_HIPPO_9182" in stdout_text, (
        f"resume did NOT continue prior session; stdout tail: {stdout_text[-400:]!r}"
    )
    print("[smoke-resume] ==> PASS: opencode resumed; keyword recovered from conversation state")
    return 0


if __name__ == "__main__":
    sys.exit(main())
