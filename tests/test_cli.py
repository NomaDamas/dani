import json
from pathlib import Path

from typer.testing import CliRunner

from dani.cli import app


def test_register_repo_and_show_state(tmp_path: Path) -> None:
    runner = CliRunner()
    data_dir = tmp_path / ".dani"

    register_result = runner.invoke(app, ["register-repo", "acme/demo", str(tmp_path), "--data-dir", str(data_dir)])
    assert register_result.exit_code == 0

    state_result = runner.invoke(app, ["show-state", "--data-dir", str(data_dir)])
    assert state_result.exit_code == 0
    payload = json.loads(state_result.stdout)
    assert payload["registry"]["repos"][0]["full_name"] == "acme/demo"
