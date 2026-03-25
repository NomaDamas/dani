from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from dani.git_sync import DevSyncConflictError, GitDevSyncer
from dani.models import JobRecord, RepoConfig


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ | {
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
    }
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(path), *args],  # noqa: S607
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    origin = tmp_path / "origin.git"
    worktree = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True, text=True)  # noqa: S603,S607
    subprocess.run(["git", "clone", str(origin), str(worktree)], check=True, capture_output=True, text=True)  # noqa: S603,S607

    (worktree / "app.txt").write_text("base\n", encoding="utf-8")
    _git(worktree, "add", "app.txt")
    _git(worktree, "commit", "-m", "initial")
    _git(worktree, "branch", "-M", "main")
    _git(worktree, "push", "-u", "origin", "main")
    _git(worktree, "checkout", "-b", "dev")
    _git(worktree, "push", "-u", "origin", "dev")
    _git(worktree, "checkout", "main")
    return worktree, str(origin)


def test_git_dev_syncer_merges_main_into_dev_and_pushes(tmp_path: Path) -> None:
    repo_path, _origin = _init_repo(tmp_path)
    (repo_path / "app.txt").write_text("base\nmain update\n", encoding="utf-8")
    _git(repo_path, "add", "app.txt")
    _git(repo_path, "commit", "-m", "main update")
    main_sha = _git(repo_path, "rev-parse", "HEAD").stdout.strip()
    _git(repo_path, "push", "origin", "main")

    syncer = GitDevSyncer(tmp_path / "runs")
    repo = RepoConfig(full_name="acme/demo", local_path=str(repo_path))
    job = JobRecord(repo_full_name=repo.full_name, stage="dev_sync", metadata={"main_sha": main_sha})

    outcome = syncer.sync(repo, job)

    assert outcome.status == "merged"
    _git(repo_path, "fetch", "origin", "dev")
    _git(repo_path, "merge-base", "--is-ancestor", main_sha, "origin/dev")


def test_git_dev_syncer_raises_conflict_error_when_merge_conflicts(tmp_path: Path) -> None:
    repo_path, _origin = _init_repo(tmp_path)

    _git(repo_path, "checkout", "dev")
    (repo_path / "app.txt").write_text("dev change\n", encoding="utf-8")
    _git(repo_path, "add", "app.txt")
    _git(repo_path, "commit", "-m", "dev change")
    _git(repo_path, "push", "origin", "dev")

    _git(repo_path, "checkout", "main")
    (repo_path / "app.txt").write_text("main change\n", encoding="utf-8")
    _git(repo_path, "add", "app.txt")
    _git(repo_path, "commit", "-m", "main change")
    main_sha = _git(repo_path, "rev-parse", "HEAD").stdout.strip()
    _git(repo_path, "push", "origin", "main")

    syncer = GitDevSyncer(tmp_path / "runs")
    repo = RepoConfig(full_name="acme/demo", local_path=str(repo_path))
    job = JobRecord(repo_full_name=repo.full_name, stage="dev_sync", metadata={"main_sha": main_sha})

    with pytest.raises(DevSyncConflictError) as exc_info:
        syncer.sync(repo, job)

    conflict_files = _git(exc_info.value.context.worktree_path, "diff", "--name-only", "--diff-filter=U").stdout
    assert "app.txt" in conflict_files
    syncer.cleanup(exc_info.value.context)
