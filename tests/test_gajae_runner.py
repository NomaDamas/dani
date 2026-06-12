from pathlib import Path

from dani.gajae_runner import (
    DEFAULT_GAJAE_FINAL_MODEL,
    DEFAULT_GAJAE_PLAN_MODEL,
    GajaeRunner,
    gajae_model_for_stage,
)
from dani.models import JobRecord


def test_gajae_model_for_issue_planning_uses_opus_plan_model() -> None:
    assert gajae_model_for_stage("issue_request") == DEFAULT_GAJAE_PLAN_MODEL
    assert gajae_model_for_stage("issue_followup") == DEFAULT_GAJAE_PLAN_MODEL


def test_gajae_model_for_final_verdict_uses_gpt_55_final_model() -> None:
    assert gajae_model_for_stage("final_verdict") == DEFAULT_GAJAE_FINAL_MODEL


def test_build_script_runs_gjc_print_headless_with_stage_model(tmp_path: Path) -> None:
    runner = GajaeRunner(run_dir=tmp_path / "runs")
    script = runner._build_script(
        repo_path=tmp_path / "repo",
        prompt_path=tmp_path / "prompt.txt",
        stage="issue_request",
    )

    assert "exec gjc --print" in script
    assert f"--model {DEFAULT_GAJAE_PLAN_MODEL}" in script
    assert f"--plan {DEFAULT_GAJAE_PLAN_MODEL}" in script


def test_launch_records_prompt_and_gajae_runtime_session(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    class _ExitedProcess:
        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

        def kill(self) -> None:
            return None

    def fake_popen(argv, **kwargs):
        del kwargs
        calls.append([str(part) for part in argv])
        return _ExitedProcess()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    runner = GajaeRunner(run_dir=tmp_path / "runs")
    repo = tmp_path / "repo"
    repo.mkdir()
    job = JobRecord(repo_full_name="acme/demo", stage="final_verdict", pr_number=12)

    session = runner.launch(repo, job, "Decide the verdict.")

    assert calls == [[session.script_path]]
    assert session.native_session_runtime == "gajae"
    assert session.effective_runtime == "gajae"
    assert session.codex_session_id is None
    assert Path(session.prompt_path).read_text(encoding="utf-8") == "Decide the verdict."
    assert Path(session.script_path).read_text(encoding="utf-8").count("gjc --print") == 1
