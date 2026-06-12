import json
import os
import subprocess
from pathlib import Path


def test_cli_register_repo_show_state_and_doctor_work_in_temp_data_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    data_dir = tmp_path / "data"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    env = os.environ.copy()
    env["DANI_AGENT_RUNTIME"] = "codex"

    register = subprocess.run(
        ["uv", "run", "dani", "register-repo", "acme/demo", str(repo), "--data-dir", str(data_dir)],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )
    assert json.loads(register.stdout)["full_name"] == "acme/demo"

    state = subprocess.run(
        ["uv", "run", "dani", "show-state", "--data-dir", str(data_dir)],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )
    assert json.loads(state.stdout)["registry"]["repos"][0]["full_name"] == "acme/demo"

    doctor = subprocess.run(
        [
            "uv",
            "run",
            "dani",
            "doctor",
            "--data-dir",
            str(data_dir),
            "--json",
            "--check",
            "storage_files",
            "--check",
            "registered_repos",
            "--check",
            "process_sprawl",
        ],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )
    payload = json.loads(doctor.stdout)
    assert payload["schema_version"] == 1
    assert payload["summary"]["fail"] == 0
