from __future__ import annotations

import sys
import time
from pathlib import Path

from dani.agent_runner import build_agent_runner
from dani.models import JobRecord

REPO_PATH = Path("/tmp/dani-omo-smoke/repo")
RUN_DIR = Path("/tmp/dani-omo-smoke/runs-ultrawork")


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
    print(f"[smoke-ulw] runner={type(runner).__name__}")

    job = JobRecord(repo_full_name="acme/smoke", stage="issue_request", issue_number=777)
    prompt = (
        "Reply with exactly the words 'ulw mode ok' and stop immediately. "
        "Do NOT enter any loop. Do NOT call any agents."
    )

    session = runner.launch(REPO_PATH, job, prompt)
    actual_prompt = Path(session.prompt_path).read_text(encoding="utf-8")
    assert actual_prompt.startswith("ultrawork\n\n"), f"expected ultrawork prefix, got: {actual_prompt[:60]!r}"
    print(f"[smoke-ulw] prompt-file starts with ultrawork prefix: {actual_prompt[:60]!r}")

    t0 = time.monotonic()
    runner.wait(session.runtime_handle, timeout_seconds=300)
    runner.close_session(session.runtime_handle)

    assert session.stdout_path is not None and session.stderr_path is not None
    stdout_text = Path(session.stdout_path).read_text(encoding="utf-8", errors="replace")
    stderr_text = Path(session.stderr_path).read_text(encoding="utf-8", errors="replace")
    assert "ses_" in stdout_text, "expected sessionID events in opencode stdout"
    assert "error" not in stderr_text.lower(), f"opencode emitted error: {stderr_text[:400]}"
    print(f"[smoke-ulw] wait: {time.monotonic() - t0:.1f}s; stderr bytes: {len(stderr_text)}")
    print("[smoke-ulw] ==> PASS: opencode accepted ultrawork-prefixed prompt and ran clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
