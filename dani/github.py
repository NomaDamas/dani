from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from github import Auth, Github
from github.GithubException import GithubException

from dani.signatures import parse_signature

TOKEN_ENV_VARS = ("DANI_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT")


class MergeConflictError(RuntimeError):
    def __init__(
        self, repo_full_name: str, pr_number: int, *, status: int | None = None, message: str | None = None
    ) -> None:
        self.repo_full_name = repo_full_name
        self.pr_number = pr_number
        self.status = status
        self.message = message or "Pull request merge failed because the branch is out of date or has conflicts."
        super().__init__(self.message)


class GitHubCLI:
    def __init__(
        self,
        *,
        token: str | None = None,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._token = token
        self._client_factory = client_factory or self._build_client
        self._client: Any | None = None

    def _build_client(self, token: str) -> Github:
        return Github(auth=Auth.Token(token))

    def _resolve_token(self) -> str:
        if self._token:
            return self._token
        for env_var in TOKEN_ENV_VARS:
            value = os.environ.get(env_var)
            if value:
                self._token = value
                return value
        msg = "GitHub token not configured. Set DANI_GITHUB_TOKEN (preferred), GITHUB_TOKEN, GH_TOKEN, or GITHUB_PAT."
        raise RuntimeError(msg)

    def _client_for_request(self) -> Any:
        if self._client is None:
            self._client = self._client_factory(self._resolve_token())
        return self._client

    def _repo(self, repo_full_name: str) -> Any:
        return self._client_for_request().get_repo(repo_full_name)

    def list_open_issues(self, repo_full_name: str) -> list[dict[str, Any]]:
        return [issue.raw_data for issue in self._repo(repo_full_name).get_issues(state="open")]

    def issue_comments(self, repo_full_name: str, issue_number: int) -> list[dict[str, Any]]:
        issue = self._repo(repo_full_name).get_issue(issue_number)
        return [comment.raw_data for comment in issue.get_comments()]

    def pr_comments(self, repo_full_name: str, pr_number: int) -> list[dict[str, Any]]:
        pull_request = self._repo(repo_full_name).get_pull(pr_number)
        return [comment.raw_data for comment in pull_request.get_issue_comments()]

    def list_pull_requests(self, repo_full_name: str) -> list[dict[str, Any]]:
        return [pull_request.raw_data for pull_request in self._repo(repo_full_name).get_pulls(state="open")]

    def get_pull_request(self, repo_full_name: str, pr_number: int) -> dict[str, Any]:
        return self._repo(repo_full_name).get_pull(pr_number).raw_data

    def find_pr_by_signature(self, repo_full_name: str, signature_fragment: str) -> dict[str, Any] | None:
        for pull_request in self.list_pull_requests(repo_full_name):
            if signature_fragment in (pull_request.get("body") or ""):
                return pull_request
        return None

    def latest_signature_comment(
        self, repo_full_name: str, number: int, *, kind: str
    ) -> tuple[dict[str, Any], dict[str, str]] | None:
        comments = (
            self.issue_comments(repo_full_name, number) if kind == "issue" else self.pr_comments(repo_full_name, number)
        )
        for comment in reversed(comments):
            parsed = parse_signature(comment.get("body", ""))
            if parsed is not None:
                return comment, parsed
        return None

    def find_comments_by_signature(
        self, repo_full_name: str, number: int, *, kind: str, signature_fragment: str
    ) -> list[dict[str, Any]]:
        comments = (
            self.issue_comments(repo_full_name, number) if kind == "issue" else self.pr_comments(repo_full_name, number)
        )
        return [comment for comment in comments if signature_fragment in (comment.get("body") or "")]

    def create_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any]:
        issue = self._repo(repo_full_name).get_issue(issue_number)
        return issue.create_comment(body).raw_data

    def create_pr_comment(self, repo_full_name: str, pr_number: int, body: str) -> dict[str, Any]:
        pull_request = self._repo(repo_full_name).get_pull(pr_number)
        return pull_request.create_issue_comment(body).raw_data

    def ensure_pull_request(
        self,
        repo_full_name: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        repo = self._repo(repo_full_name)
        owner, _repo_name = repo_full_name.split("/", 1)
        existing_pull_requests = list(repo.get_pulls(state="open", head=f"{owner}:{head}", base=base))
        if existing_pull_requests:
            pull_request = existing_pull_requests[0]
            pull_request.edit(title=title, body=body, base=base)
            return pull_request.raw_data
        return repo.create_pull(title=title, body=body, base=base, head=head).raw_data

    def merge_pull_request(self, repo_full_name: str, pr_number: int) -> None:
        pull_request = self._repo(repo_full_name).get_pull(pr_number)
        try:
            pull_request.merge(merge_method="merge", delete_branch=True)
        except GithubException as exc:
            if exc.status in {405, 409}:
                raise MergeConflictError(repo_full_name, pr_number, status=exc.status, message=str(exc)) from exc
            raise
