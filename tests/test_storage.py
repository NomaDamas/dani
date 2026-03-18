from pathlib import Path

from dani.models import DaniConfig, JobRecord, RepoConfig, SessionRecord
from dani.storage import JsonStorage

TEST_SECRET = "unit-test-secret"


def test_storage_persists_registry_jobs_sessions_and_events(tmp_path: Path) -> None:
    config = DaniConfig(data_dir=tmp_path / ".dani", webhook_secret=TEST_SECRET)
    storage = JsonStorage(config)

    repo = RepoConfig(full_name="acme/demo", local_path=str(tmp_path))
    storage.register_repo(repo)
    created_job = storage.create_job(JobRecord(repo_full_name=repo.full_name, stage="issue_request", issue_number=1))
    storage.create_session(
        SessionRecord(
            repo_full_name=repo.full_name,
            stage="issue_request",
            tmux_session="tmux-1",
            prompt_path=str(tmp_path / "prompt.txt"),
            script_path=str(tmp_path / "run.sh"),
            worktree_path=str(tmp_path),
            job_id=created_job.id,
        )
    )
    storage.append_event({"kind": "issue_opened", "repo_full_name": repo.full_name})

    snapshot = storage.snapshot()

    assert snapshot["registry"]["repos"][0]["full_name"] == "acme/demo"
    assert snapshot["jobs"]["jobs"][0]["stage"] == "issue_request"
    assert snapshot["sessions"]["sessions"][0]["tmux_session"] == "tmux-1"
    assert config.events_path.read_text(encoding="utf-8").strip()
