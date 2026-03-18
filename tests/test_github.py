from __future__ import annotations

import pytest

from dani.github import GitHubCLI


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


class FakePullRequest:
    def __init__(self, *, number: int = 7, body: str = "", comments: list[str] | None = None) -> None:
        self.number = number
        self.body = body
        self.title = f"PR #{number}"
        self.comments = [FakeComment(comment) for comment in comments or []]
        self.created_issue_comments: list[str] = []
        self.edits: list[dict[str, str]] = []
        self.merged = False
        self.raw_data = {"number": number, "body": body, "title": self.title}

    def get_issue_comments(self) -> list[FakeComment]:
        return list(self.comments)

    def create_issue_comment(self, body: str) -> FakeComment:
        self.created_issue_comments.append(body)
        return FakeComment(body)

    def edit(self, *, title: str, body: str, base: str) -> None:
        self.title = title
        self.body = body
        self.edits.append({"title": title, "body": body, "base": base})
        self.raw_data = {"number": self.number, "body": body, "title": title}

    def merge(self, *, merge_method: str, delete_branch: bool) -> None:
        assert merge_method == "merge"
        assert delete_branch is True
        self.merged = True


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
        pull_request = FakePullRequest(number=99, body=body)
        pull_request.title = title
        pull_request.raw_data = {"number": 99, "body": body, "title": title}
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
