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
            codex_session_id="ses_omo123",
            preferred_runtime="omo",
            effective_runtime="omo",
            native_session_runtime="omo",
        )
    )
    storage.create_session(
        SessionRecord(
            repo_full_name=repo.full_name,
            stage="issue_request",
            runtime_handle="runtime-codex",
            prompt_path=str(tmp_path / "prompt-codex.txt"),
            script_path=str(tmp_path / "run-codex.sh"),
            worktree_path=str(tmp_path),
            job_id=job.id,
            issue_number=1,
            codex_session_id="codex-123",
            preferred_runtime="omo",
            effective_runtime="codex",
            native_session_runtime="codex",
            fallback_reason="claude_weekly_limit",
            bridge_source_runtime="omo",
            bridge_source_session_id="ses_omo123",
        )
    )

    latest_codex = storage.find_latest_session(
        repo_full_name=repo.full_name,
        issue_number=1,
        require_codex_session_id=True,
        effective_runtime="codex",
    )

    assert latest_codex is not None
    assert latest_codex.fallback_reason == "claude_weekly_limit"
    assert latest_codex.bridge_source_runtime == "omo"
    assert latest_codex.bridge_source_session_id == "ses_omo123"


def test_storage_reads_legacy_codex_session_key(tmp_path: Path) -> None:
    config = DaniConfig(data_dir=tmp_path / ".dani", webhook_secret=TEST_SECRET)
    storage = JsonStorage(config)
    legacy_session_key = f"{'o'}{'mx'}_session_id"
    config.sessions_path.write_text(
        (
            "{\n"
            '  "sessions": [\n'
            "    {\n"
            '      "repo_full_name": "acme/demo",\n'
            '      "stage": "issue_request",\n'
            '      "runtime_handle": "runtime-1",\n'
            '      "prompt_path": "prompt.txt",\n'
            '      "script_path": "run.sh",\n'
            '      "worktree_path": "repo",\n'
            '      "job_id": "job-1",\n'
            '      "id": "session-1",\n'
            f'      "{legacy_session_key}": "codex-legacy"\n'
            "    }\n"
            "  ]\n"
            "}\n"
        ),
        encoding="utf-8",
    )

    session = storage.list_sessions()[0]

    assert session.codex_session_id == "codex-legacy"


def test_find_latest_session_can_filter_by_source_job_id(tmp_path: Path) -> None:
    config = DaniConfig(data_dir=tmp_path / ".dani", webhook_secret=TEST_SECRET)
    storage = JsonStorage(config)
    repo = RepoConfig(full_name="acme/demo", local_path=str(tmp_path))
    storage.register_repo(repo)
    first_job = storage.create_job(JobRecord(repo_full_name=repo.full_name, stage="issue_request", issue_number=40))
    second_job = storage.create_job(JobRecord(repo_full_name=repo.full_name, stage="issue_request", issue_number=40))
    storage.create_session(
        SessionRecord(
            repo_full_name=repo.full_name,
            stage="issue_request",
            runtime_handle="runtime-first",
            prompt_path=str(tmp_path / "prompt-first.txt"),
            script_path=str(tmp_path / "run-first.sh"),
            worktree_path=str(tmp_path),
            job_id=first_job.id,
            issue_number=40,
            codex_session_id="codex-first",
        )
    )
    storage.create_session(
        SessionRecord(
            repo_full_name=repo.full_name,
            stage="issue_request",
            runtime_handle="runtime-second",
            prompt_path=str(tmp_path / "prompt-second.txt"),
            script_path=str(tmp_path / "run-second.sh"),
            worktree_path=str(tmp_path),
            job_id=second_job.id,
            issue_number=40,
            codex_session_id="codex-second",
        )
    )

    source_session = storage.find_latest_session(
        repo_full_name=repo.full_name,
        stage="issue_request",
        issue_number=40,
        job_id=first_job.id,
    )

    assert source_session is not None
    assert source_session.job_id == first_job.id
    assert source_session.codex_session_id == "codex-first"
