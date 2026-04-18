from __future__ import annotations

import json
from pathlib import Path

import pytest

from dani.errors import RolloutMissingError, TransientCapacityError
from dani.omo_runner import OmoRunner


def test_build_script_uses_opencode_run(tmp_path: Path) -> None:
    runner = OmoRunner(run_dir=tmp_path / "runs")
    script = runner._build_script(repo_path=tmp_path / "repo", prompt_path=tmp_path / "prompt.txt")

    assert "opencode run --format json --dangerously-skip-permissions" in script
    assert "cd " in script


def test_build_resume_script_uses_opencode_session_flag(tmp_path: Path) -> None:
    runner = OmoRunner(run_dir=tmp_path / "runs")
    script = runner._build_resume_script(
        repo_path=tmp_path / "repo",
        prompt_path=tmp_path / "prompt.txt",
        opencode_session_id="ses_abc123",
    )

    assert "opencode run --session ses_abc123 --format json --dangerously-skip-permissions" in script


def test_build_script_honours_custom_binary(tmp_path: Path) -> None:
    runner = OmoRunner(run_dir=tmp_path / "runs", opencode_bin="/usr/local/bin/opencode")
    script = runner._build_script(repo_path=tmp_path / "repo", prompt_path=tmp_path / "prompt.txt")

    assert "/usr/local/bin/opencode run --format json" in script


def test_session_id_from_stdout_parses_sessionid_field(tmp_path: Path) -> None:
    stdout_path = tmp_path / "stdout.log"
    stdout_path.write_text(
        json.dumps({
            "type": "step_start",
            "timestamp": 1776528793789,
            "sessionID": "ses_25ea1f4beffeDiVTHAxqiPJa4G",
            "part": {"type": "step-start"},
        })
        + "\n",
        encoding="utf-8",
    )
    runner = OmoRunner(run_dir=tmp_path / "runs")

    assert runner._session_id_from_stdout(stdout_path) == "ses_25ea1f4beffeDiVTHAxqiPJa4G"


def test_session_id_from_stdout_ignores_malformed_lines(tmp_path: Path) -> None:
    stdout_path = tmp_path / "stdout.log"
    stdout_path.write_text(
        "\n".join([
            "not-json",
            json.dumps({"type": "noise", "other": "value"}),
            json.dumps({"type": "text", "sessionID": "ses_realone123"}),
        ])
        + "\n",
        encoding="utf-8",
    )
    runner = OmoRunner(run_dir=tmp_path / "runs")

    assert runner._session_id_from_stdout(stdout_path) == "ses_realone123"


def test_session_id_from_stdout_returns_none_when_no_event(tmp_path: Path) -> None:
    stdout_path = tmp_path / "stdout.log"
    stdout_path.write_text("", encoding="utf-8")
    runner = OmoRunner(run_dir=tmp_path / "runs")

    assert runner._session_id_from_stdout(stdout_path) is None


def test_capture_session_id_polls_until_visible(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    stdout_path = runs_dir / "stdout.log"
    stdout_path.write_text(
        json.dumps({"type": "step_start", "sessionID": "ses_polled"}) + "\n",
        encoding="utf-8",
    )
    runner = OmoRunner(run_dir=runs_dir)

    result = runner._capture_session_id(stdout_path=stdout_path, poll_interval=0.01, timeout_seconds=0.05)

    assert result == "ses_polled"


def test_capture_session_id_returns_none_on_timeout(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    stdout_path = runs_dir / "stdout.log"
    stdout_path.write_text("", encoding="utf-8")
    runner = OmoRunner(run_dir=runs_dir)

    result = runner._capture_session_id(stdout_path=stdout_path, poll_interval=0.01, timeout_seconds=0.03)

    assert result is None


def test_close_session_terminates_active_process(tmp_path: Path) -> None:
    runner = OmoRunner(run_dir=tmp_path / "runs")
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    stdout_file = stdout_path.open("w", encoding="utf-8")
    stderr_file = stderr_path.open("w", encoding="utf-8")

    process = type(
        "Process",
        (),
        {
            "poll": lambda self: None,
            "terminate": lambda self: None,
            "wait": lambda self, timeout=None: 0,
            "kill": lambda self: None,
        },
    )()
    runner._processes["runtime-123"] = (process, stdout_file, stderr_file)

    runner.close_session("runtime-123")

    assert stdout_file.closed
    assert stderr_file.closed
    assert runner._processes == {}


def test_close_session_skips_missing_process(tmp_path: Path) -> None:
    runner = OmoRunner(run_dir=tmp_path / "runs")
    runner.close_session("runtime-123")


def test_wait_raises_rollout_missing_error_when_stderr_mentions_session_not_found(tmp_path: Path) -> None:
    runner = OmoRunner(run_dir=tmp_path / "runs")
    runtime_handle = "runtime-missing"
    runtime_dir = runner.run_dir / runtime_handle
    runtime_dir.mkdir(parents=True)
    stdout_path = runtime_dir / "stdout.log"
    stderr_path = runtime_dir / "stderr.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("Error: Session not found: ses_missing123", encoding="utf-8")
    stdout_file = stdout_path.open("w", encoding="utf-8")
    stderr_file = stderr_path.open("a", encoding="utf-8")
    process = type(
        "Process",
        (),
        {
            "poll": lambda self: 1,
            "terminate": lambda self: None,
            "wait": lambda self, timeout=None: 1,
            "kill": lambda self: None,
        },
    )()
    runner._processes[runtime_handle] = (process, stdout_file, stderr_file)

    try:
        with pytest.raises(RolloutMissingError, match="Session not found"):
            runner.wait(runtime_handle)
    finally:
        stdout_file.close()
        stderr_file.close()


def test_wait_raises_transient_capacity_when_stderr_matches_pattern(tmp_path: Path) -> None:
    runner = OmoRunner(run_dir=tmp_path / "runs")
    runtime_handle = "runtime-capacity"
    runtime_dir = runner.run_dir / runtime_handle
    runtime_dir.mkdir(parents=True)
    stdout_path = runtime_dir / "stdout.log"
    stderr_path = runtime_dir / "stderr.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("Selected model is at capacity, retrying...", encoding="utf-8")
    stdout_file = stdout_path.open("w", encoding="utf-8")
    stderr_file = stderr_path.open("a", encoding="utf-8")
    process = type(
        "Process",
        (),
        {
            "poll": lambda self: 1,
            "terminate": lambda self: None,
            "wait": lambda self, timeout=None: 1,
            "kill": lambda self: None,
        },
    )()
    runner._processes[runtime_handle] = (process, stdout_file, stderr_file)

    try:
        with pytest.raises(TransientCapacityError):
            runner.wait(runtime_handle)
    finally:
        stdout_file.close()
        stderr_file.close()


def test_launch_writes_prompt_file_with_exact_prompt_content(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dani.models import JobRecord

    runner = OmoRunner(run_dir=tmp_path / "runs")
    captured_prompts: list[str] = []

    class FakeProcess:
        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    def fake_popen(cmd, **kwargs):
        script_path = Path(cmd[0])
        script_text = script_path.read_text(encoding="utf-8")
        prompt_line = next((line for line in script_text.splitlines() if "cat " in line), "")
        prompt_file = Path(prompt_line.rsplit("cat ", 1)[1].rstrip(')"'))
        captured_prompts.append(prompt_file.read_text(encoding="utf-8"))
        kwargs["stdout"].close()
        kwargs["stderr"].close()
        return FakeProcess()

    monkeypatch.setattr("dani.omo_runner.subprocess.Popen", fake_popen)
    monkeypatch.setattr(runner, "_capture_session_id", lambda **kwargs: None)

    job = JobRecord(repo_full_name="acme/demo", stage="issue_request", issue_number=1)
    runner.launch(tmp_path / "repo", job, "HELLO EXACT PROMPT BODY 12345")

    assert captured_prompts == ["HELLO EXACT PROMPT BODY 12345"]


def test_resume_writes_prompt_file_with_exact_prompt_content(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dani.models import JobRecord

    runner = OmoRunner(run_dir=tmp_path / "runs")
    captured_prompts: list[str] = []

    class FakeProcess:
        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    def fake_popen(cmd, **kwargs):
        script_path = Path(cmd[0])
        script_text = script_path.read_text(encoding="utf-8")
        prompt_line = next((line for line in script_text.splitlines() if "cat " in line), "")
        prompt_file = Path(prompt_line.rsplit("cat ", 1)[1].rstrip(')"'))
        captured_prompts.append(prompt_file.read_text(encoding="utf-8"))
        kwargs["stdout"].close()
        kwargs["stderr"].close()
        return FakeProcess()

    monkeypatch.setattr("dani.omo_runner.subprocess.Popen", fake_popen)

    job = JobRecord(repo_full_name="acme/demo", stage="issue_followup", issue_number=1)
    runner.resume(tmp_path / "repo", job, "RESUME EXACT PROMPT 67890", "ses_persisted")

    assert captured_prompts == ["RESUME EXACT PROMPT 67890"]


def test_get_session_id_returns_parsed_id_from_stdout_log(tmp_path: Path) -> None:
    runner = OmoRunner(run_dir=tmp_path / "runs")
    runtime_handle = "dani-issue_request-xyz"
    runtime_dir = runner.run_dir / runtime_handle
    runtime_dir.mkdir(parents=True)
    stdout_path = runtime_dir / "stdout.log"
    stdout_path.write_text(
        json.dumps({"type": "step_start", "sessionID": "ses_postwaitrecovered01"}) + "\n",
        encoding="utf-8",
    )

    assert runner.get_session_id(runtime_handle) == "ses_postwaitrecovered01"


def test_get_session_id_returns_none_when_stdout_missing(tmp_path: Path) -> None:
    runner = OmoRunner(run_dir=tmp_path / "runs")
    assert runner.get_session_id("dani-issue_request-missing") is None


def test_omx_runner_get_session_id_returns_none(tmp_path: Path) -> None:
    from dani.omx_runner import OmxRunner

    omx = OmxRunner(run_dir=tmp_path / "omx-runs")
    assert omx.get_session_id("any-handle") is None


def test_dani_service_recovers_late_session_id_from_omo_runner_after_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """issue_request runs; _capture_session_id times out; wait() completes; service re-queries runner and updates storage."""
    from typing import cast

    from dani.github import GitHubCLI
    from dani.models import DaniConfig, NormalizedEvent
    from dani.service import DaniService
    from dani.storage import JsonStorage
    from tests.helpers import FakeGitDevSyncer, FakeGitHubCLI

    class FakeProcess:
        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    github_stub = FakeGitHubCLI()
    late_session_id = "ses_latecapture000000000abcdef"

    def fake_popen(cmd, **kwargs):
        script_path = Path(cmd[0])
        script_text = script_path.read_text(encoding="utf-8")
        prompt_line = next((line for line in script_text.splitlines() if "cat " in line), "")
        prompt_file = Path(prompt_line.rsplit("cat ", 1)[1].rstrip(')"'))
        prompt_body = prompt_file.read_text(encoding="utf-8")

        stdout_path = Path(kwargs["stdout"].name)
        stderr_path = Path(kwargs["stderr"].name)
        stdout_path.write_text(
            json.dumps({"type": "step_start", "sessionID": late_session_id}) + "\n",
            encoding="utf-8",
        )
        stderr_path.write_text("", encoding="utf-8")
        kwargs["stdout"].close()
        kwargs["stderr"].close()

        signature = next(
            (fragment for fragment in prompt_body.splitlines() if fragment.startswith("<!-- dani:")),
            None,
        )
        if signature is not None:
            github_stub.issue_comment_map.setdefault(("acme/demo", 77), []).append({"body": signature})
        return FakeProcess()

    monkeypatch.setattr("dani.omo_runner.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "dani.omo_runner.OmoRunner._capture_session_id",
        lambda self, **kwargs: None,
    )

    config = DaniConfig(
        data_dir=tmp_path / ".dani",
        webhook_secret="unit-test-secret",
        agent_runtime="omo",
    )
    storage = JsonStorage(config)
    service = DaniService(
        config,
        storage=storage,
        github=cast(GitHubCLI, github_stub),
        dev_syncer=FakeGitDevSyncer(),
    )
    service.register_repo("acme/demo", str(tmp_path))

    service.handle_event(
        NormalizedEvent(
            kind="issue_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=77,
            actor_login="human",
            payload={},
            body="Simulate slow session-id emission",
            title="Late-capture smoke",
        )
    )
    service.wait_for_idle()

    sessions = storage.list_sessions()
    assert sessions
    assert sessions[0].omx_session_id == late_session_id, (
        "service must re-query runner post-wait and update session with late-arriving id"
    )


def test_build_agent_runner_factory_returns_omo_runner(tmp_path: Path) -> None:
    from dani.agent_runner import build_agent_runner

    runner = build_agent_runner("omo", tmp_path / "runs")

    assert isinstance(runner, OmoRunner)


def test_build_agent_runner_factory_returns_omx_runner_by_default(tmp_path: Path) -> None:
    from dani.agent_runner import build_agent_runner
    from dani.omx_runner import OmxRunner

    runner = build_agent_runner("omx", tmp_path / "runs")

    assert isinstance(runner, OmxRunner)


def test_build_agent_runner_rejects_unknown_runtime(tmp_path: Path) -> None:
    from dani.agent_runner import build_agent_runner

    with pytest.raises(ValueError, match="unknown agent runtime"):
        build_agent_runner("nonsense", tmp_path / "runs")


def test_build_agent_runner_rejects_ultrawork_with_omx(tmp_path: Path) -> None:
    from dani.agent_runner import build_agent_runner

    with pytest.raises(ValueError, match="ultrawork mode is only supported"):
        build_agent_runner("omx", tmp_path / "runs", ultrawork=True)


def test_build_agent_runner_propagates_ultrawork_flag_to_omo(tmp_path: Path) -> None:
    from dani.agent_runner import build_agent_runner

    runner = build_agent_runner("omo", tmp_path / "runs", ultrawork=True)

    assert isinstance(runner, OmoRunner)
    assert runner.ultrawork is True


def test_omo_runner_prepends_ultrawork_prefix_when_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dani.models import JobRecord

    runner = OmoRunner(run_dir=tmp_path / "runs", ultrawork=True)
    captured_prompts: list[str] = []

    class FakeProcess:
        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    def fake_popen(cmd, **kwargs):
        script_path = Path(cmd[0])
        script_text = script_path.read_text(encoding="utf-8")
        prompt_line = next((line for line in script_text.splitlines() if "cat " in line), "")
        prompt_file = Path(prompt_line.rsplit("cat ", 1)[1].rstrip(')"'))
        captured_prompts.append(prompt_file.read_text(encoding="utf-8"))
        kwargs["stdout"].close()
        kwargs["stderr"].close()
        return FakeProcess()

    monkeypatch.setattr("dani.omo_runner.subprocess.Popen", fake_popen)
    monkeypatch.setattr(runner, "_capture_session_id", lambda **kwargs: None)

    job = JobRecord(repo_full_name="acme/demo", stage="issue_request", issue_number=1)
    runner.launch(tmp_path / "repo", job, "Investigate Issue #33.")

    assert captured_prompts == ["ultrawork\n\nInvestigate Issue #33."]


def test_omo_runner_does_not_double_prefix_when_prompt_already_starts_with_ultrawork(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dani.models import JobRecord

    runner = OmoRunner(run_dir=tmp_path / "runs", ultrawork=True)
    captured_prompts: list[str] = []

    class FakeProcess:
        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    def fake_popen(cmd, **kwargs):
        script_path = Path(cmd[0])
        script_text = script_path.read_text(encoding="utf-8")
        prompt_line = next((line for line in script_text.splitlines() if "cat " in line), "")
        prompt_file = Path(prompt_line.rsplit("cat ", 1)[1].rstrip(')"'))
        captured_prompts.append(prompt_file.read_text(encoding="utf-8"))
        kwargs["stdout"].close()
        kwargs["stderr"].close()
        return FakeProcess()

    monkeypatch.setattr("dani.omo_runner.subprocess.Popen", fake_popen)
    monkeypatch.setattr(runner, "_capture_session_id", lambda **kwargs: None)

    job = JobRecord(repo_full_name="acme/demo", stage="issue_request", issue_number=1)
    runner.launch(tmp_path / "repo", job, "ultrawork already leading")

    assert captured_prompts == ["ultrawork already leading"]


def test_omo_runner_resume_also_applies_ultrawork_prefix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dani.models import JobRecord

    runner = OmoRunner(run_dir=tmp_path / "runs", ultrawork=True)
    captured_prompts: list[str] = []

    class FakeProcess:
        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    def fake_popen(cmd, **kwargs):
        script_path = Path(cmd[0])
        script_text = script_path.read_text(encoding="utf-8")
        prompt_line = next((line for line in script_text.splitlines() if "cat " in line), "")
        prompt_file = Path(prompt_line.rsplit("cat ", 1)[1].rstrip(')"'))
        captured_prompts.append(prompt_file.read_text(encoding="utf-8"))
        kwargs["stdout"].close()
        kwargs["stderr"].close()
        return FakeProcess()

    monkeypatch.setattr("dani.omo_runner.subprocess.Popen", fake_popen)

    job = JobRecord(repo_full_name="acme/demo", stage="issue_followup", issue_number=1)
    runner.resume(tmp_path / "repo", job, "Continue fixing", "ses_persisted_omo")

    assert captured_prompts == ["ultrawork\n\nContinue fixing"]


def test_cli_build_config_reads_dani_agent_ultrawork_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dani.cli import build_config

    monkeypatch.setenv("DANI_WEBHOOK_SECRET", "secret-ignored")
    monkeypatch.setenv("DANI_AGENT_RUNTIME", "omo")
    monkeypatch.setenv("DANI_AGENT_ULTRAWORK", "true")

    config = build_config(data_dir=tmp_path)

    assert config.agent_runtime == "omo"
    assert config.agent_ultrawork is True


def test_cli_build_config_default_ultrawork_is_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dani.cli import build_config

    monkeypatch.setenv("DANI_WEBHOOK_SECRET", "secret-ignored")
    monkeypatch.delenv("DANI_AGENT_ULTRAWORK", raising=False)

    config = build_config(data_dir=tmp_path)

    assert config.agent_ultrawork is False


def test_dani_service_instantiates_omo_runner_when_config_agent_runtime_is_omo(tmp_path: Path) -> None:
    from dani.models import DaniConfig
    from dani.service import DaniService

    config = DaniConfig(
        data_dir=tmp_path / ".dani",
        webhook_secret="unit-test-secret",
        agent_runtime="omo",
    )
    service = DaniService(config)

    assert isinstance(service.omx_runner, OmoRunner)


def test_dani_service_defaults_to_omx_runner_for_backward_compat(tmp_path: Path) -> None:
    from dani.models import DaniConfig
    from dani.omx_runner import OmxRunner
    from dani.service import DaniService

    config = DaniConfig(data_dir=tmp_path / ".dani", webhook_secret="unit-test-secret")
    service = DaniService(config)

    assert isinstance(service.omx_runner, OmxRunner)


def test_dani_service_runs_issue_request_through_omo_runner_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typing import cast

    from dani.github import GitHubCLI
    from dani.models import DaniConfig, NormalizedEvent
    from dani.service import DaniService
    from dani.storage import JsonStorage
    from tests.helpers import FakeGitDevSyncer, FakeGitHubCLI

    class FakeProcess:
        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    github_stub = FakeGitHubCLI()
    captured_scripts: list[str] = []

    def fake_popen(cmd, **kwargs):
        script_path = Path(cmd[0])
        script_text = script_path.read_text(encoding="utf-8")
        captured_scripts.append(script_text)
        stdout_path = Path(kwargs["stdout"].name)
        stderr_path = Path(kwargs["stderr"].name)
        import json as _json

        session_id = "ses_stubbedomo1234567890abcdef"
        event = {"type": "step_start", "timestamp": 0, "sessionID": session_id, "part": {"type": "step-start"}}
        stdout_path.write_text(_json.dumps(event) + "\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        kwargs["stdout"].close()
        kwargs["stderr"].close()
        prompt_line = next((line for line in script_text.splitlines() if "cat " in line), "")
        prompt_file = Path(prompt_line.rsplit("cat ", 1)[1].rstrip(')"'))
        prompt_body = prompt_file.read_text(encoding="utf-8")
        signature = next(
            (fragment for fragment in prompt_body.splitlines() if fragment.startswith("<!-- dani:")),
            None,
        )
        if signature is not None:
            github_stub.issue_comment_map.setdefault(("acme/demo", 99), []).append({"body": signature})
        return FakeProcess()

    monkeypatch.setattr("dani.omo_runner.subprocess.Popen", fake_popen)

    config = DaniConfig(
        data_dir=tmp_path / ".dani",
        webhook_secret="unit-test-secret",
        agent_runtime="omo",
    )
    storage = JsonStorage(config)
    service = DaniService(
        config,
        storage=storage,
        github=cast(GitHubCLI, github_stub),
        dev_syncer=FakeGitDevSyncer(),
    )
    service.register_repo("acme/demo", str(tmp_path))

    assert isinstance(service.omx_runner, OmoRunner)

    service.handle_event(
        NormalizedEvent(
            kind="issue_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=99,
            actor_login="human",
            payload={},
            body="Investigate runtime selection",
            title="Runtime selection smoke",
        )
    )
    service.wait_for_idle()

    job = storage.list_jobs()[0]
    assert job.status == "completed", f"expected completed, got {job.status} / {job.metadata!r}"
    assert captured_scripts, "expected at least one opencode run invocation"
    assert "opencode run --format json --dangerously-skip-permissions" in captured_scripts[0]

    sessions = storage.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].omx_session_id == "ses_stubbedomo1234567890abcdef"


def test_dani_service_resumes_opencode_session_on_issue_followup_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """issue_opened -> persisted sessionID -> issue_comment -> OmoRunner.resume with --session flag."""
    from typing import cast

    from dani.github import GitHubCLI
    from dani.models import DaniConfig, NormalizedEvent
    from dani.service import DaniService
    from dani.storage import JsonStorage
    from tests.helpers import FakeGitDevSyncer, FakeGitHubCLI

    class FakeProcess:
        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    github_stub = FakeGitHubCLI()
    scripts_executed: list[str] = []
    prompts_executed: list[str] = []
    captured_session_id = "ses_resumechain0000000000abcd"

    def fake_popen(cmd, **kwargs):
        script_path = Path(cmd[0])
        script_text = script_path.read_text(encoding="utf-8")
        scripts_executed.append(script_text)
        prompt_line = next((line for line in script_text.splitlines() if "cat " in line), "")
        prompt_file = Path(prompt_line.rsplit("cat ", 1)[1].rstrip(')"'))
        prompt_body = prompt_file.read_text(encoding="utf-8")
        prompts_executed.append(prompt_body)

        stdout_path = Path(kwargs["stdout"].name)
        stderr_path = Path(kwargs["stderr"].name)
        import json as _json

        event = {"type": "step_start", "timestamp": 0, "sessionID": captured_session_id, "part": {}}
        stdout_path.write_text(_json.dumps(event) + "\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        kwargs["stdout"].close()
        kwargs["stderr"].close()

        signature = next(
            (fragment for fragment in prompt_body.splitlines() if fragment.startswith("<!-- dani:")),
            None,
        )
        if signature is not None:
            github_stub.issue_comment_map.setdefault(("acme/demo", 42), []).append({"body": signature})
        return FakeProcess()

    monkeypatch.setattr("dani.omo_runner.subprocess.Popen", fake_popen)

    config = DaniConfig(
        data_dir=tmp_path / ".dani",
        webhook_secret="unit-test-secret",
        agent_runtime="omo",
    )
    storage = JsonStorage(config)
    service = DaniService(
        config,
        storage=storage,
        github=cast(GitHubCLI, github_stub),
        dev_syncer=FakeGitDevSyncer(),
    )
    service.register_repo("acme/demo", str(tmp_path))

    assert isinstance(service.omx_runner, OmoRunner)

    service.handle_event(
        NormalizedEvent(
            kind="issue_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=42,
            actor_login="human",
            payload={},
            body="Initial issue body for OMO resume chain",
            title="OMO resume chain smoke",
        )
    )
    service.wait_for_idle()

    first_job = storage.list_jobs()[0]
    assert first_job.status == "completed"
    persisted_session = storage.list_sessions()[0]
    assert persisted_session.omx_session_id == captured_session_id

    service.handle_event(
        NormalizedEvent(
            kind="issue_comment",
            repo_full_name="acme/demo",
            action="created",
            number=42,
            actor_login="human",
            payload={"issue": {"body": "Initial issue body for OMO resume chain"}},
            body="Actually also look at subsystem Y.",
            title="OMO resume chain smoke",
        )
    )
    service.wait_for_idle()

    followup_jobs = [job for job in storage.list_jobs() if job.stage == "issue_followup"]
    assert followup_jobs, "issue_followup job should have been enqueued"
    followup_job = followup_jobs[-1]
    assert followup_job.status == "completed", (
        f"expected completed, got {followup_job.status} with metadata {followup_job.metadata!r}"
    )
    assert followup_job.metadata.get("omx_session_id") == captured_session_id

    followup_scripts = [script for script in scripts_executed if "--session" in script]
    assert followup_scripts, "resume path should have generated an opencode run --session script"
    assert (
        f"opencode run --session {captured_session_id} --format json --dangerously-skip-permissions"
        in (followup_scripts[-1])
    )

    followup_sessions = [session for session in storage.list_sessions() if session.stage == "issue_followup"]
    assert followup_sessions and followup_sessions[-1].omx_session_id == captured_session_id


def test_dani_service_handles_opencode_session_missing_on_followup_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typing import cast

    from dani.github import GitHubCLI
    from dani.models import DaniConfig, NormalizedEvent
    from dani.service import DaniService
    from dani.storage import JsonStorage
    from tests.helpers import FakeGitDevSyncer, FakeGitHubCLI

    class FakeProcess:
        def __init__(self, exit_code: int = 0) -> None:
            self._exit_code = exit_code

        def poll(self) -> int:
            return self._exit_code

        def wait(self, timeout: float | None = None) -> int:
            return self._exit_code

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    github_stub = FakeGitHubCLI()
    invocation_count = {"count": 0}
    captured_session_id = "ses_missingchain000000000ffff"

    def fake_popen(cmd, **kwargs):
        invocation_count["count"] += 1
        script_path = Path(cmd[0])
        script_text = script_path.read_text(encoding="utf-8")
        prompt_line = next((line for line in script_text.splitlines() if "cat " in line), "")
        prompt_file = Path(prompt_line.rsplit("cat ", 1)[1].rstrip(')"'))
        prompt_body = prompt_file.read_text(encoding="utf-8")

        stdout_path = Path(kwargs["stdout"].name)
        stderr_path = Path(kwargs["stderr"].name)
        import json as _json

        if "--session" in script_text:
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(f"Error: Session not found: {captured_session_id}\n", encoding="utf-8")
            kwargs["stdout"].close()
            kwargs["stderr"].close()
            return FakeProcess(exit_code=1)

        event = {"type": "step_start", "timestamp": 0, "sessionID": captured_session_id, "part": {}}
        stdout_path.write_text(_json.dumps(event) + "\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        kwargs["stdout"].close()
        kwargs["stderr"].close()

        signature = next(
            (fragment for fragment in prompt_body.splitlines() if fragment.startswith("<!-- dani:")),
            None,
        )
        if signature is not None:
            github_stub.issue_comment_map.setdefault(("acme/demo", 88), []).append({"body": signature})
        return FakeProcess(exit_code=0)

    monkeypatch.setattr("dani.omo_runner.subprocess.Popen", fake_popen)

    config = DaniConfig(
        data_dir=tmp_path / ".dani",
        webhook_secret="unit-test-secret",
        agent_runtime="omo",
    )
    storage = JsonStorage(config)
    service = DaniService(
        config,
        storage=storage,
        github=cast(GitHubCLI, github_stub),
        dev_syncer=FakeGitDevSyncer(),
    )
    service.register_repo("acme/demo", str(tmp_path))

    service.handle_event(
        NormalizedEvent(
            kind="issue_opened",
            repo_full_name="acme/demo",
            action="opened",
            number=88,
            actor_login="human",
            payload={},
            body="Initial",
            title="Session-missing smoke",
        )
    )
    service.wait_for_idle()

    service.handle_event(
        NormalizedEvent(
            kind="issue_comment",
            repo_full_name="acme/demo",
            action="created",
            number=88,
            actor_login="human",
            payload={"issue": {"body": "Initial"}},
            body="Ping",
            title="Session-missing smoke",
        )
    )
    service.wait_for_idle()

    followup_jobs = [job for job in storage.list_jobs() if job.stage == "issue_followup"]
    assert followup_jobs, "followup job should have been enqueued"
    followup_job = followup_jobs[-1]
    assert followup_job.status == "failed"
    assert followup_job.metadata.get("error") == "rollout_missing"
    warning_comments = [
        comment
        for comment in github_stub.issue_comment_map.get(("acme/demo", 88), [])
        if "stage=session_lost" in comment.get("body", "")
    ]
    assert warning_comments, "service should have posted the session_lost warning comment"
