"""Live verification of OmoHttpRunner. Requires CCAPI_API_KEY/MYPROXY_API_KEY.

Run with: uv run python scripts/dev/verify_http_runner_live.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from dani.models import JobRecord  # noqa: E402
from dani.omo_http_runner import OmoHttpRunner  # noqa: E402
from dani.opencode_http import OpencodeClient, OpencodeServerManager  # noqa: E402


def _emit(test_id: str, message: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"{ts} {test_id} {message}", flush=True)


@contextmanager
def temp_repo() -> Any:
    with tempfile.TemporaryDirectory(prefix="dani-verify-repo-") as repo_dir:
        repo_path = Path(repo_dir)
        (repo_path / "AGENTS.md").write_text("# verification scratch\n", encoding="utf-8")
        (repo_path / "hello.txt").write_text("dani verification\n", encoding="utf-8")
        yield repo_path


def test1_server_lifecycle() -> dict[str, Any]:
    test_id = "TEST1"
    _emit(test_id, "spawning opencode serve via OpencodeServerManager")
    with tempfile.TemporaryDirectory(prefix="dani-verify-runs-") as run_dir, temp_repo() as repo_path:
        manager = OpencodeServerManager(Path(run_dir))
        base_url = manager.get_server_for_repo(repo_path)
        _emit(test_id, f"server URL: {base_url}")

        client = OpencodeClient(base_url)

        statuses = client.session_status()
        _emit(test_id, f"initial /session/status: {statuses!r}")
        assert isinstance(statuses, dict), "session_status must return a dict"

        info = client.create_session(directory=str(repo_path), title="dani-verify-lifecycle")
        _emit(test_id, f"created session: {info.id} (directory={info.directory})")
        assert info.id.startswith("ses_"), "session id must start with ses_"
        assert info.directory == str(repo_path) or info.directory.endswith(repo_path.name), (
            f"directory mismatch: got {info.directory}, expected ~ {repo_path}"
        )

        aborted = client.abort_session(info.id)
        _emit(test_id, f"abort returned: {aborted}")
        assert aborted is True

        post_abort_statuses = client.session_status()
        _emit(test_id, f"post-abort /session/status: {post_abort_statuses!r}")

        process = manager._servers[str(repo_path.resolve())].process
        assert process is not None and process.poll() is None, "server died during test"
        _emit(test_id, f"server pid={process.pid} still alive after abort -> shutting down")

        manager.shutdown_all()
        time.sleep(0.5)
        assert process.poll() is not None, "server did not exit on shutdown_all"
        _emit(test_id, f"server exited cleanly with returncode={process.poll()}")

        return {
            "url": base_url,
            "session_id": info.id,
            "exit_code": process.poll(),
        }


def _read_events(event_log_path: str | None) -> list[dict[str, Any]]:
    if not event_log_path:
        return []
    path = Path(event_log_path)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _summarize_events(events: list[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for event in events:
        kind = str(event.get("type", "?"))
        summary[kind] = summary.get(kind, 0) + 1
    return summary


def _parent_idle_seen(events: list[dict[str, Any]], parent_session_id: str) -> bool:
    return any(
        e.get("type") == "session.idle" and (e.get("properties") or {}).get("sessionID") == parent_session_id
        for e in events
    )


def _child_session_created(events: list[dict[str, Any]], parent_session_id: str) -> list[str]:
    child_ids: list[str] = []
    for event in events:
        if event.get("type") != "session.created":
            continue
        props = event.get("properties") or {}
        info = props.get("info") or {}
        if isinstance(info, dict) and info.get("parentID") == parent_session_id:
            sid = info.get("id")
            if isinstance(sid, str):
                child_ids.append(sid)
    return child_ids


def test2_session_management() -> dict[str, Any]:
    test_id = "TEST2"
    with tempfile.TemporaryDirectory(prefix="dani-verify-runs-") as run_dir, temp_repo() as repo_path:
        runner = OmoHttpRunner(Path(run_dir))
        try:
            _emit(test_id, "launch with simple deterministic prompt")
            launch_job = JobRecord(
                repo_full_name="live/verify",
                stage="implementation",
                issue_number=2001,
            )
            prompt_a = (
                "Verification ping #1. Reply with EXACTLY the single word 'session-ack-1' "
                "and immediately stop. Do not use any tools. Do not enter any loop. "
                "This is a smoke test."
            )
            t0 = time.monotonic()
            session_a = runner.launch(repo_path, launch_job, prompt_a)
            _emit(test_id, f"launch returned in {time.monotonic() - t0:.2f}s; session_id={session_a.codex_session_id}")
            assert session_a.codex_session_id and session_a.codex_session_id.startswith("ses_")

            _emit(test_id, "wait for session idle (timeout=180s)")
            t0 = time.monotonic()
            runner.wait(session_a.runtime_handle, timeout_seconds=180)
            _emit(test_id, f"wait returned in {time.monotonic() - t0:.2f}s")

            events_a = _read_events(session_a.stdout_path)
            summary_a = _summarize_events(events_a)
            _emit(test_id, f"events seen for session A: {summary_a}")
            assert _parent_idle_seen(events_a, session_a.codex_session_id), (
                "parent session.idle event was never recorded in event log"
            )
            runner.close_session(session_a.runtime_handle)

            _emit(test_id, "resume same session with follow-up prompt")
            resume_job = JobRecord(
                repo_full_name="live/verify",
                stage="implementation",
                issue_number=2001,
            )
            prompt_b = (
                "Verification ping #2 in the SAME session. What was the EXACT word you replied with "
                "in your previous turn? Reply with ONLY that word, nothing else, then stop."
            )
            t0 = time.monotonic()
            session_b = runner.resume(repo_path, resume_job, prompt_b, session_a.codex_session_id)
            _emit(test_id, f"resume returned in {time.monotonic() - t0:.2f}s; session_id={session_b.codex_session_id}")
            assert session_b.codex_session_id == session_a.codex_session_id, (
                f"resume should target same session: got {session_b.codex_session_id}, "
                f"expected {session_a.codex_session_id}"
            )

            t0 = time.monotonic()
            runner.wait(session_b.runtime_handle, timeout_seconds=180)
            _emit(test_id, f"resume-wait returned in {time.monotonic() - t0:.2f}s")

            events_b = _read_events(session_b.stdout_path)
            text_parts: list[str] = []
            for event in events_b:
                if event.get("type") != "message.part.updated":
                    continue
                props = event.get("properties") or {}
                part = props.get("part") or {}
                if part.get("type") == "text":
                    text_parts.append(str(part.get("text", "")))
            joined = " ".join(text_parts).lower()
            _emit(test_id, f"resume reply text snapshot (truncated): {joined[:200]!r}")
            assert "session-ack-1" in joined, (
                f"resume must reference prior turn keyword 'session-ack-1'; saw {joined[:300]!r}"
            )
            runner.close_session(session_b.runtime_handle)

            return {
                "session_id": session_a.codex_session_id,
                "events_seen_a": summary_a,
                "resume_continuity_keyword_present": True,
            }
        finally:
            runner.shutdown()


def test3_ultrawork_subagents() -> dict[str, Any]:
    test_id = "TEST3"
    with tempfile.TemporaryDirectory(prefix="dani-verify-runs-") as run_dir, temp_repo() as repo_path:
        runner = OmoHttpRunner(Path(run_dir))
        try:
            _emit(test_id, "launch with ultrawork prompt that demands ONE quick subagent call via task()")
            job = JobRecord(
                repo_full_name="live/verify",
                stage="implementation",
                issue_number=3001,
            )
            prompt = (
                "ultrawork\n\n"
                "VERIFICATION TEST FOR SUBAGENT SPAWNING. Do EXACTLY this: invoke the `task` tool "
                "ONCE with the following arguments to delegate a tiny subtask:\n"
                "  - category: 'quick'\n"
                "  - load_skills: []\n"
                "  - run_in_background: false\n"
                "  - description: 'subagent ack'\n"
                "  - prompt: 'Reply with EXACTLY the word: subagent-ack. Stop immediately.'\n"
                "After the subagent returns, reply with EXACTLY the single word 'parent-ack' and stop. "
                "Do not loop, do not call any other tool, do not delegate again. "
                "This is a verification test of the HTTP server subagent path."
            )
            t0 = time.monotonic()
            session = runner.launch(repo_path, job, prompt)
            _emit(
                test_id,
                f"launch returned in {time.monotonic() - t0:.2f}s; parent session_id={session.codex_session_id}",
            )
            assert session.codex_session_id and session.codex_session_id.startswith("ses_")

            base_url_before = runner._server_manager.get_server_for_repo(repo_path)
            try:
                req = urllib.request.Request(f"{base_url_before}/path")  # noqa: S310
                with urllib.request.urlopen(req, timeout=5) as response:  # noqa: S310
                    _ = response.read(1)
                _emit(test_id, "server reachable BEFORE wait (HTTP 200 on /path)")
            except urllib.error.URLError as exc:
                msg = f"server unreachable before wait: {exc}"
                raise RuntimeError(msg) from exc

            _emit(test_id, "wait for parent session.idle (timeout=300s)")
            t0 = time.monotonic()
            runner.wait(session.runtime_handle, timeout_seconds=300)
            _emit(test_id, f"wait returned in {time.monotonic() - t0:.2f}s")

            try:
                req = urllib.request.Request(f"{base_url_before}/path")  # noqa: S310
                with urllib.request.urlopen(req, timeout=5) as response:  # noqa: S310
                    _ = response.read(1)
                _emit(test_id, "server STILL reachable AFTER subagent run (HTTP 200 on /path)")
            except urllib.error.URLError as exc:
                msg = f"server died during subagent run: {exc}"
                raise RuntimeError(msg) from exc

            events = _read_events(session.stdout_path)
            summary = _summarize_events(events)
            _emit(test_id, f"events seen: {summary}")

            child_session_ids = _child_session_created(events, session.codex_session_id)
            _emit(test_id, f"child sessions spawned with parentID={session.codex_session_id}: {child_session_ids}")
            assert child_session_ids, (
                "no child session.created events with parentID matching parent — subagent did not spawn"
            )

            assert _parent_idle_seen(events, session.codex_session_id), (
                "parent session.idle was never recorded in event log"
            )

            for child_id in child_session_ids:
                child_idle = any(
                    e.get("type") == "session.idle" and (e.get("properties") or {}).get("sessionID") == child_id
                    for e in events
                )
                _emit(test_id, f"child {child_id} idle observed: {child_idle}")

            runner.close_session(session.runtime_handle)
            return {
                "parent_session_id": session.codex_session_id,
                "child_session_ids": child_session_ids,
                "events_summary": summary,
            }
        finally:
            runner.shutdown()


def main() -> int:
    started = time.monotonic()
    results: dict[str, Any] = {}
    failures: list[str] = []

    for test_id, fn in [
        ("TEST1", test1_server_lifecycle),
        ("TEST2", test2_session_management),
        ("TEST3", test3_ultrawork_subagents),
    ]:
        print(f"\n{'=' * 72}\n{test_id}\n{'=' * 72}", flush=True)
        t0 = time.monotonic()
        try:
            result = fn()
            duration = time.monotonic() - t0
            results[test_id] = {"status": "PASS", "duration_seconds": round(duration, 2), "data": result}
            print(f"\n{test_id} PASS in {duration:.1f}s", flush=True)
        except Exception as exc:
            duration = time.monotonic() - t0
            results[test_id] = {"status": "FAIL", "duration_seconds": round(duration, 2), "error": repr(exc)}
            failures.append(f"{test_id}: {exc!r}")
            print(f"\n{test_id} FAIL in {duration:.1f}s: {exc!r}", flush=True)

    total = time.monotonic() - started
    print("\n" + "=" * 72, flush=True)
    print(f"FINAL VERDICT: {'PASS' if not failures else 'FAIL'} (total {total:.1f}s)", flush=True)
    print(json.dumps(results, indent=2), flush=True)
    if failures:
        print("\nFAILURES:", flush=True)
        for f in failures:
            print(f"  - {f}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
