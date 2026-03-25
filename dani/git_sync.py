from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from dani.models import JobRecord, RepoConfig


@dataclass(slots=True)
class DevSyncContext:
    repo_path: Path
    worktree_path: Path
    source_branch: str
    target_branch: str
    source_sha: str
    temp_branch: str


@dataclass(slots=True)
class DevSyncOutcome:
    status: str


class DevSyncConflictError(RuntimeError):
    def __init__(self, context: DevSyncContext) -> None:
        super().__init__("dev-sync-conflict")
        self.context = context


class GitDevSyncer:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def sync(self, repo: RepoConfig, job: JobRecord) -> DevSyncOutcome:
        source_sha = self._source_sha_for(job)
        repo_path = Path(repo.local_path)
        self._run_git(repo_path, "fetch", "origin", repo.main_branch, repo.dev_branch)
        if self._is_ancestor(repo_path, source_sha, f"origin/{repo.dev_branch}"):
            return DevSyncOutcome(status="already_up_to_date")

        context = self._prepare_context(repo, job, source_sha)
        try:
            merge_result = self._run_git(
                context.worktree_path,
                "merge",
                "--no-ff",
                "--no-commit",
                source_sha,
                check=False,
            )
            if merge_result.returncode == 0:
                if self._has_pending_merge(context.worktree_path):
                    self._commit_merge(context, self.build_commit_message(repo, job))
                    self._push(context)
                    self.verify_remote_sync(context)
                    self.cleanup(context)
                    return DevSyncOutcome(status="merged")
                self.verify_remote_sync(context)
                self.cleanup(context)
                return DevSyncOutcome(status="already_up_to_date")
            if self._has_conflicts(context.worktree_path):
                self._raise_conflict(context)
            message = merge_result.stderr.strip() or merge_result.stdout.strip() or "git-merge-failed"
            self._raise_runtime_error(message)
        except DevSyncConflictError:
            raise
        except Exception:
            self.cleanup(context)
            raise

    def build_commit_message(self, repo: RepoConfig, job: JobRecord) -> str:
        source_sha = self._source_sha_for(job)
        return "\n".join([
            f"Keep {repo.dev_branch} aligned with {repo.main_branch} after upstream updates",
            "",
            f"Sync {repo.main_branch} commit {source_sha} into {repo.dev_branch} so the",
            "development branch stays current with the latest mainline history.",
            "",
            f"Constraint: {repo.dev_branch} accepts direct pushes from dani automation",
            "Constraint: merge commits must follow the Lore commit protocol",
            "Rejected: Open a sync PR first | direct pushes are allowed for clean repository sync",
            "Confidence: high",
            "Scope-risk: narrow",
            f"Directive: Resolve conflicts in the dedicated worktree before pushing to {repo.dev_branch}",
            f"Tested: git merge {source_sha} into {repo.dev_branch} worktree and pushed when clean",
            "Not-tested: Project test suite execution during automated branch synchronization",
        ])

    def verify_remote_sync(self, context: DevSyncContext) -> None:
        self._run_git(context.repo_path, "fetch", "origin", context.source_branch, context.target_branch)
        self._run_git(context.worktree_path, "diff", "--name-only", "--diff-filter=U")
        self._run_git(
            context.repo_path, "merge-base", "--is-ancestor", context.source_sha, f"origin/{context.target_branch}"
        )

    def cleanup(self, context: DevSyncContext) -> None:
        self._run_git(context.repo_path, "worktree", "remove", "--force", str(context.worktree_path), check=False)
        self._run_git(context.repo_path, "worktree", "prune", check=False)

    def _prepare_context(self, repo: RepoConfig, job: JobRecord, source_sha: str) -> DevSyncContext:
        repo_path = Path(repo.local_path)
        worktree_path = self.run_dir / f"dev-sync-{job.id}"
        temp_branch = f"dani/dev-sync/{job.id}"
        if worktree_path.exists():
            self._run_git(repo_path, "worktree", "remove", "--force", str(worktree_path), check=False)
        self._run_git(repo_path, "worktree", "add", "--detach", str(worktree_path), f"origin/{repo.dev_branch}")
        self._run_git(worktree_path, "checkout", "-B", temp_branch, f"origin/{repo.dev_branch}")
        return DevSyncContext(
            repo_path=repo_path,
            worktree_path=worktree_path,
            source_branch=repo.main_branch,
            target_branch=repo.dev_branch,
            source_sha=source_sha,
            temp_branch=temp_branch,
        )

    def _commit_merge(self, context: DevSyncContext, commit_message: str) -> None:
        commit_message_path = context.worktree_path / ".dani-dev-sync-commit-message.txt"
        commit_message_path.write_text(commit_message, encoding="utf-8")
        env = os.environ | {
            "GIT_AUTHOR_NAME": "dani",
            "GIT_AUTHOR_EMAIL": "dani@example.com",
            "GIT_COMMITTER_NAME": "dani",
            "GIT_COMMITTER_EMAIL": "dani@example.com",
        }
        self._run_git(context.worktree_path, "commit", "--file", str(commit_message_path), env=env)

    def _push(self, context: DevSyncContext) -> None:
        self._run_git(context.worktree_path, "push", "origin", f"HEAD:refs/heads/{context.target_branch}")

    def _has_pending_merge(self, repo_path: Path) -> bool:
        result = self._run_git(repo_path, "rev-parse", "--verify", "MERGE_HEAD", check=False)
        return result.returncode == 0

    def _has_conflicts(self, repo_path: Path) -> bool:
        result = self._run_git(repo_path, "diff", "--name-only", "--diff-filter=U", check=False)
        return bool(result.stdout.strip())

    def _is_ancestor(self, repo_path: Path, ancestor: str, descendant: str) -> bool:
        result = self._run_git(repo_path, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
        return result.returncode == 0

    def _source_sha_for(self, job: JobRecord) -> str:
        source_sha = job.metadata.get("main_sha")
        if isinstance(source_sha, str) and source_sha:
            return source_sha
        msg = "missing-main-sha"
        raise RuntimeError(msg)

    def _raise_conflict(self, context: DevSyncContext) -> NoReturn:
        raise DevSyncConflictError(context)

    def _raise_runtime_error(self, message: str) -> NoReturn:
        raise RuntimeError(message)

    def _run_git(
        self,
        repo_path: Path,
        *args: str,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["git", "-C", str(repo_path), *args],  # noqa: S607
            check=check,
            capture_output=True,
            text=True,
            env=env,
        )
