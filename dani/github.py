from __future__ import annotations

import json
import subprocess
from typing import Any

from dani.signatures import parse_signature


class GitHubCLI:
    def _run(self, args: list[str]) -> str:
        completed = subprocess.run(args, check=True, capture_output=True, text=True)  # noqa: S603
        return completed.stdout

    def api_json(self, *args: str) -> Any:
        payload = self._run(["gh", "api", *args])
        return json.loads(payload)

    def list_open_issues(self, repo_full_name: str) -> list[dict[str, Any]]:
        return self.api_json(f"repos/{repo_full_name}/issues", "-f", "state=open")

    def issue_comments(self, repo_full_name: str, issue_number: int) -> list[dict[str, Any]]:
        return self.api_json(f"repos/{repo_full_name}/issues/{issue_number}/comments")

    def pr_comments(self, repo_full_name: str, pr_number: int) -> list[dict[str, Any]]:
        return self.api_json(f"repos/{repo_full_name}/issues/{pr_number}/comments")

    def list_pull_requests(self, repo_full_name: str) -> list[dict[str, Any]]:
        return self.api_json(f"repos/{repo_full_name}/pulls", "-f", "state=open")

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

    def merge_pull_request(self, repo_full_name: str, pr_number: int) -> None:
        self._run(["gh", "pr", "merge", str(pr_number), "--repo", repo_full_name, "--merge", "--delete-branch"])
