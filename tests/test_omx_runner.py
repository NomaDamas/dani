from __future__ import annotations

import json
import time
from pathlib import Path

from dani.omx_runner import OmxRunner
from dani.signatures import build_signature


def test_capture_omx_session_id_matches_signature_and_repo_path(tmp_path: Path) -> None:
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
                    "originator": "codex-tui",
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
