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
            runtime_handle="runtime-1",
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
    assert snapshot["sessions"]["sessions"][0]["runtime_handle"] == "runtime-1"
    assert config.events_path.read_text(encoding="utf-8").strip()


def test_storage_round_trips_runtime_metadata_and_filters_by_effective_runtime(tmp_path: Path) -> None:
    config = DaniConfig(data_dir=tmp_path / ".dani", webhook_secret=TEST_SECRET)
    storage = JsonStorage(config)
    repo = RepoConfig(full_name="acme/demo", local_path=str(tmp_path))
    storage.register_repo(repo)
    job = storage.create_job(JobRecord(repo_full_name=repo.full_name, stage="issue_request", issue_number=1))
    storage.create_session(
        SessionRecord(
            repo_full_name=repo.full_name,
            stage="issue_request",
            runtime_handle="runtime-omo",
            prompt_path=str(tmp_path / "prompt-omo.txt"),
            script_path=str(tmp_path / "run-omo.sh"),
            worktree_path=str(tmp_path),
            job_id=job.id,
            issue_number=1,
            omx_session_id="ses_omo123",
            preferred_runtime="omo",
            effective_runtime="omo",
            native_session_runtime="omo",
        )
    )
    storage.create_session(
        SessionRecord(
            repo_full_name=repo.full_name,
            stage="issue_request",
            runtime_handle="runtime-omx",
            prompt_path=str(tmp_path / "prompt-omx.txt"),
            script_path=str(tmp_path / "run-omx.sh"),
            worktree_path=str(tmp_path),
            job_id=job.id,
            issue_number=1,
            omx_session_id="omx-123",
            preferred_runtime="omo",
            effective_runtime="omx",
            native_session_runtime="omx",
            fallback_reason="claude_weekly_limit",
            bridge_source_runtime="omo",
            bridge_source_session_id="ses_omo123",
        )
    )

    latest_omx = storage.find_latest_session(
        repo_full_name=repo.full_name,
        issue_number=1,
        require_omx_session_id=True,
        effective_runtime="omx",
    )

    assert latest_omx is not None
    assert latest_omx.fallback_reason == "claude_weekly_limit"
    assert latest_omx.bridge_source_runtime == "omo"
    assert latest_omx.bridge_source_session_id == "ses_omo123"
