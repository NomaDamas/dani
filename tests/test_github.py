from __future__ import annotations

import pytest
from github.GithubException import GithubException

from dani.github import GitHubCLI, MergeConflictError


class FakeComment:
    def __init__(self, body: str) -> None:
        self.body = body
        self.raw_data = {"body": body}


class FakeIssue:
    def __init__(self, comments: list[str] | None = None) -> None:
        self.comments = [FakeComment(body) for body in comments or []]
        self.created_comments: list[str] = []
        self.raw_data = {"number": 1}

    def get_comments(self) -> list[FakeComment]:
        return list(self.comments)

    def create_comment(self, body: str) -> FakeComment:
        self.created_comments.append(body)
        return FakeComment(body)


class FakeBranchRef:
    def __init__(self, ref: str) -> None:
        self.ref = ref


class FakeMergeStatus:
    def __init__(self, *, merged: bool = True, message: str = "merged") -> None:
        self.merged = merged
        self.message = message


class FakePullRequest:
    def __init__(
        self,
        *,
        number: int = 7,
        body: str = "",
        comments: list[str] | None = None,
        head_ref: str | None = None,
        base_ref: str = "dev",
    ) -> None:
        self.number = number
        self.body = body
        self.title = f"PR #{number}"
        self.head = FakeBranchRef(head_ref or f"feature/#{number}")
        self.base = FakeBranchRef(base_ref)
        self.comments = [FakeComment(comment) for comment in comments or []]
        self.created_issue_comments: list[str] = []
        self.edits: list[dict[str, str]] = []
        self.merged = False
        self.merge_exception: Exception | None = None
        self.merge_status = FakeMergeStatus()
        self.delete_branch_exception: Exception | None = None
        self.delete_branch_calls = 0
        self._refresh_raw_data()

    def _refresh_raw_data(self) -> None:
        self.raw_data = {
            "number": self.number,
            "body": self.body,
            "title": self.title,
            "head": {"ref": self.head.ref},
            "base": {"ref": self.base.ref},
        }

    def get_issue_comments(self) -> list[FakeComment]:
        return list(self.comments)

    def create_issue_comment(self, body: str) -> FakeComment:
        self.created_issue_comments.append(body)
        return FakeComment(body)

    def edit(self, *, title: str, body: str, base: str) -> None:
        self.title = title
        self.body = body
        self.base = FakeBranchRef(base)
        self.edits.append({"title": title, "body": body, "base": base})
        self._refresh_raw_data()

    def merge(self, *, merge_method: str, delete_branch: bool) -> FakeMergeStatus:
        assert merge_method == "merge"
        assert delete_branch is False
        if self.merge_exception is not None:
            raise self.merge_exception
        self.merged = self.merge_status.merged
        return self.merge_status

    def delete_branch(self) -> None:
        self.delete_branch_calls += 1
        if self.delete_branch_exception is not None:
            raise self.delete_branch_exception


class FakeRepo:
    def __init__(self) -> None:
        self.issues = {5: FakeIssue(["existing issue comment"])}
        self.pulls = {7: FakePullRequest(number=7, body="body", comments=["existing pr comment"])}
        self.created_pulls: list[dict[str, str]] = []

    def get_issues(self, *, state: str) -> list[FakeIssue]:
        assert state == "open"
        return list(self.issues.values())

    def get_issue(self, issue_number: int) -> FakeIssue:
        return self.issues[issue_number]

    def get_pull(self, pr_number: int) -> FakePullRequest:
        return self.pulls[pr_number]

    def get_pulls(self, *, state: str, head: str | None = None, base: str | None = None) -> list[FakePullRequest]:
        assert state == "open"
        if head == "acme:feature/#5" and base == "dev":
            return [self.pulls[7]]
        return list(self.pulls.values()) if head is None and base is None else []

    def create_pull(self, *, title: str, body: str, base: str, head: str) -> FakePullRequest:
        payload = {"title": title, "body": body, "base": base, "head": head}
        self.created_pulls.append(payload)
        pull_request = FakePullRequest(number=99, body=body, head_ref=head, base_ref=base)
        pull_request.title = title
        pull_request._refresh_raw_data()
        self.pulls[99] = pull_request
        return pull_request


class FakeClient:
    def __init__(self, repo: FakeRepo) -> None:
        self.repo = repo
        self.requested_repos: list[str] = []

    def get_repo(self, repo_full_name: str) -> FakeRepo:
        self.requested_repos.append(repo_full_name)
        return self.repo


@pytest.fixture
def fake_repo() -> FakeRepo:
    return FakeRepo()


def test_prefers_dani_github_token_env_var(monkeypatch: pytest.MonkeyPatch, fake_repo: FakeRepo) -> None:
    used_tokens: list[str] = []

    def client_factory(token: str) -> FakeClient:
        used_tokens.append(token)
        return FakeClient(fake_repo)

    monkeypatch.setenv("DANI_GITHUB_TOKEN", "preferred-token")
    monkeypatch.setenv("GITHUB_TOKEN", "fallback-token")

    github = GitHubCLI(client_factory=client_factory)

    github.list_open_issues("acme/demo")

    assert used_tokens == ["preferred-token"]


def test_create_issue_and_pr_comments_use_repository_objects(fake_repo: FakeRepo) -> None:
    github = GitHubCLI(token="unit-test-token", client_factory=lambda _token: FakeClient(fake_repo))

    issue_comment = github.create_issue_comment("acme/demo", 5, "hello issue")
    pr_comment = github.create_pr_comment("acme/demo", 7, "hello pr")

    assert issue_comment["body"] == "hello issue"
    assert pr_comment["body"] == "hello pr"
    assert fake_repo.issues[5].created_comments == ["hello issue"]
    assert fake_repo.pulls[7].created_issue_comments == ["hello pr"]


def test_ensure_pull_request_updates_existing_open_pull_request(fake_repo: FakeRepo) -> None:
    github = GitHubCLI(token="unit-test-token", client_factory=lambda _token: FakeClient(fake_repo))

    pull_request = github.ensure_pull_request(
        "acme/demo",
        head="feature/#5",
        base="dev",
        title="Feature/#5",
        body="updated body",
    )

    assert pull_request["number"] == 7
    assert fake_repo.pulls[7].edits == [{"title": "Feature/#5", "body": "updated body", "base": "dev"}]
    assert fake_repo.created_pulls == []


def test_get_pull_request_returns_raw_pull_request_payload(fake_repo: FakeRepo) -> None:
    github = GitHubCLI(token="unit-test-token", client_factory=lambda _token: FakeClient(fake_repo))

    pull_request = github.get_pull_request("acme/demo", 7)

    assert pull_request["number"] == 7
    assert pull_request["head"]["ref"] == "feature/#7"
    assert pull_request["base"]["ref"] == "dev"


@pytest.mark.parametrize("status", [405, 409, 422])
def test_merge_pull_request_raises_merge_conflict_error_for_merge_api_failures(
    fake_repo: FakeRepo, status: int
) -> None:
    fake_repo.pulls[7].merge_exception = GithubException(
        status,
        {"message": "Base branch was modified. Review and try the merge again."},
        {},
    )
    github = GitHubCLI(token="unit-test-token", client_factory=lambda _token: FakeClient(fake_repo))

    with pytest.raises(MergeConflictError) as exc_info:
        github.merge_pull_request("acme/demo", 7)

    assert exc_info.value.status == status


def test_merge_pull_request_raises_merge_conflict_error_when_merge_status_is_false(fake_repo: FakeRepo) -> None:
    fake_repo.pulls[7].merge_status = FakeMergeStatus(merged=False, message="Pull Request is not mergeable")
    github = GitHubCLI(token="unit-test-token", client_factory=lambda _token: FakeClient(fake_repo))

    with pytest.raises(MergeConflictError, match="Pull Request is not mergeable"):
        github.merge_pull_request("acme/demo", 7)

    assert fake_repo.pulls[7].delete_branch_calls == 0


def test_merge_pull_request_allows_branch_delete_failure_after_success(fake_repo: FakeRepo) -> None:
    fake_repo.pulls[7].delete_branch_exception = RuntimeError("branch delete failed")
    github = GitHubCLI(token="unit-test-token", client_factory=lambda _token: FakeClient(fake_repo))

    github.merge_pull_request("acme/demo", 7)

    assert fake_repo.pulls[7].merged is True
    assert fake_repo.pulls[7].delete_branch_calls == 1
