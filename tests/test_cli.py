import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import dani.cli as cli_module
from dani.agent_runner import normalize_runtime
from dani.cli import app
from dani.models import JobRecord


class FakeBootstrapService:
    def __init__(self, count: int = 2) -> None:
        self.count = count
        self.calls: list[tuple[str, str | None]] = []

    def bootstrap_repo(self, repo_full_name: str) -> int:
        self.calls.append(("bootstrap_repo", repo_full_name))
        return self.count

    def wait_for_idle(self) -> None:
        self.calls.append(("wait_for_idle", None))


class FakeRestartService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int] | tuple[str, None]] = []

    def restart_issue(self, repo_full_name: str, issue_number: int) -> JobRecord:
        self.calls.append(("restart_issue", repo_full_name, issue_number))
        return JobRecord(repo_full_name=repo_full_name, stage="issue_request", issue_number=issue_number, id="job-123")

    def wait_for_idle(self) -> None:
        self.calls.append(("wait_for_idle", None))


def test_register_repo_and_show_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    data_dir = tmp_path / ".dani"
    monkeypatch.delenv("DANI_AGENT_RUNTIME", raising=False)

    register_result = runner.invoke(app, ["register-repo", "acme/demo", str(tmp_path), "--data-dir", str(data_dir)])
    assert register_result.exit_code == 0

    state_result = runner.invoke(app, ["show-state", "--data-dir", str(data_dir)])
    assert state_result.exit_code == 0
    payload = json.loads(state_result.stdout)
    assert payload["registry"]["repos"][0]["full_name"] == "acme/demo"


def test_bootstrap_waits_for_idle_before_exiting(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    data_dir = tmp_path / ".dani"
    fake_service = FakeBootstrapService(count=2)
    monkeypatch.setattr(cli_module, "build_service", lambda data_dir: fake_service)

    result = runner.invoke(app, ["bootstrap", "acme/demo", "--data-dir", str(data_dir)])

    assert result.exit_code == 0
    assert fake_service.calls == [("bootstrap_repo", "acme/demo"), ("wait_for_idle", None)]
    assert result.stdout.strip() == "processed 2 issues"


def test_restart_issue_invokes_service_waits_for_idle_and_prints_job(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    data_dir = tmp_path / ".dani"
    fake_service = FakeRestartService()
    monkeypatch.setattr(cli_module, "build_service", lambda data_dir: fake_service)

    result = runner.invoke(app, ["restart-issue", "acme/demo", "41", "--data-dir", str(data_dir)])

    assert result.exit_code == 0
    assert fake_service.calls == [("restart_issue", "acme/demo", 41), ("wait_for_idle", None)]
    payload = json.loads(result.stdout)
    assert payload["id"] == "job-123"
    assert payload["stage"] == "issue_request"
    assert payload["issue_number"] == 41


def test_build_config_reads_agent_timeout_from_config_file(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    (data_dir / "config.json").write_text(json.dumps({"agent_timeout_seconds": 7200}), encoding="utf-8")
    monkeypatch.delenv("DANI_AGENT_TIMEOUT_SECONDS", raising=False)

    config = cli_module.build_config(data_dir)

    assert config.agent_timeout_seconds == 7200


def test_build_config_agent_timeout_env_overrides_config_file(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    (data_dir / "config.json").write_text(json.dumps({"agent_timeout_seconds": 7200}), encoding="utf-8")
    monkeypatch.setenv("DANI_AGENT_TIMEOUT_SECONDS", "5400")

    config = cli_module.build_config(data_dir)

    assert config.agent_timeout_seconds == 5400


def test_build_config_defaults_to_codex_runtime(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    monkeypatch.delenv("DANI_AGENT_RUNTIME", raising=False)

    config = cli_module.build_config(data_dir)

    assert config.agent_runtime == "codex"


def test_normalize_runtime_rejects_removed_alias() -> None:
    assert normalize_runtime(None) == "codex"
    assert normalize_runtime("codex") == "codex"
    rejected_alias = "o" + "mx"
    with pytest.raises(ValueError, match="unknown agent runtime"):
        normalize_runtime(rejected_alias)
