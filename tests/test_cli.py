import json
from pathlib import Path

from typer.testing import CliRunner

import dani.cli as cli_module
from dani.cli import app


class FakeBootstrapService:
    def __init__(self, count: int = 2) -> None:
        self.count = count
        self.calls: list[tuple[str, str | None]] = []

    def bootstrap_repo(self, repo_full_name: str) -> int:
        self.calls.append(("bootstrap_repo", repo_full_name))
        return self.count

    def wait_for_idle(self) -> None:
        self.calls.append(("wait_for_idle", None))


def test_register_repo_and_show_state(tmp_path: Path) -> None:
    runner = CliRunner()
    data_dir = tmp_path / ".dani"

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
