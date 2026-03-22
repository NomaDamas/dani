from __future__ import annotations

import re
from pathlib import Path
from typing import Any, TypedDict

from dani.models import JobRecord, SessionRecord
from dani.signatures import build_signature, parse_signature


class FakeGitHubCLI:
    def __init__(self) -> None:
        self.issue_comment_map: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.pr_comment_map: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.prs: dict[str, list[dict[str, Any]]] = {}
        self.open_issues: dict[str, list[dict[str, Any]]] = {}
        self.merged: list[tuple[str, int]] = []

    def list_open_issues(self, repo_full_name: str) -> list[dict[str, Any]]:
        return list(self.open_issues.get(repo_full_name, []))

    def issue_comments(self, repo_full_name: str, issue_number: int) -> list[dict[str, Any]]:
        return list(self.issue_comment_map.get((repo_full_name, issue_number), []))

    def pr_comments(self, repo_full_name: str, pr_number: int) -> list[dict[str, Any]]:
        return list(self.pr_comment_map.get((repo_full_name, pr_number), []))

    def list_pull_requests(self, repo_full_name: str) -> list[dict[str, Any]]:
        return list(self.prs.get(repo_full_name, []))

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
        self.merged.append((repo_full_name, pr_number))

    def add_issue_signature(self, repo_full_name: str, issue_number: int, signature: str) -> None:
        self.issue_comment_map.setdefault((repo_full_name, issue_number), []).append({"body": signature})

    def add_pr_signature(self, repo_full_name: str, pr_number: int, signature: str) -> None:
        self.pr_comment_map.setdefault((repo_full_name, pr_number), []).append({"body": signature})

    def add_pull_request(self, repo_full_name: str, pr_number: int, signature: str) -> None:
        self.prs.setdefault(repo_full_name, []).append({"number": pr_number, "body": signature})


class LaunchRecord(TypedDict):
    repo_path: str
    job: JobRecord
    prompt: str


class ResumeRecord(TypedDict):
    repo_path: str
    job: JobRecord
    prompt: str
    omx_session_id: str


class FakeOmxRunner:
    def __init__(self, github: FakeGitHubCLI) -> None:
        self.github = github
        self.launches: list[LaunchRecord] = []
        self.resumes: list[ResumeRecord] = []

    def launch(self, repo_path: Path, job: JobRecord, prompt: str) -> SessionRecord:
        repo_full_name = job.repo_full_name
        matches = re.findall(r"<!--\s*dani:([^>]+)\s*-->", prompt)
        signature = None
        if matches:
            signature = parse_signature(f"<!-- dani:{matches[-1]} -->")
        if job.stage == "issue_request":
            issue_number = int((signature or {}).get("issue", job.issue_number or 0))
            self.github.add_issue_signature(
                repo_full_name,
                issue_number,
                build_signature(stage="issue_request", job=job.id, issue=issue_number),
            )
        elif job.stage == "implementation":
            issue_number = int((signature or {}).get("issue", job.issue_number or 0))
            self.github.add_pull_request(
                repo_full_name,
                101,
                build_signature(stage="implementation", job=job.id, issue=issue_number),
            )
        elif job.stage == "review_round":
            pr_number = int((signature or {}).get("pr", job.pr_number or 0))
            self.github.add_pr_signature(
                repo_full_name,
                pr_number,
                build_signature(stage="review_round", job=job.id, pr=pr_number, round=job.review_round or 1),
            )
        else:
            self.github.add_pr_signature(
                repo_full_name,
                job.pr_number or 0,
                build_signature(stage="final_verdict", job=job.id, pr=job.pr_number or 0, verdict="APPROVE"),
            )

        self.launches.append({"repo_path": str(repo_path), "job": job, "prompt": prompt})
        return SessionRecord(
            repo_full_name=repo_full_name,
            stage=job.stage,
            tmux_session=f"tmux-{job.id}",
            pane_id="%1",
            prompt_path=str(repo_path / "prompt.txt"),
            script_path=str(repo_path / "run.sh"),
            worktree_path=str(repo_path),
            job_id=job.id,
            issue_number=job.issue_number,
            pr_number=job.pr_number,
            review_round=job.review_round,
            omx_session_id=f"omx-{job.id}",
        )

    def resume(self, repo_path: Path, job: JobRecord, prompt: str, omx_session_id: str) -> SessionRecord:
        issue_number = job.issue_number or 0
        self.github.add_issue_signature(
            job.repo_full_name,
            issue_number,
            build_signature(stage="issue_followup", job=job.id, issue=issue_number),
        )
        self.resumes.append({
            "repo_path": str(repo_path),
            "job": job,
            "prompt": prompt,
            "omx_session_id": omx_session_id,
        })
        return SessionRecord(
            repo_full_name=job.repo_full_name,
            stage=job.stage,
            tmux_session=f"tmux-{job.id}",
            pane_id="%1",
            prompt_path=str(repo_path / "prompt.txt"),
            script_path=str(repo_path / "run.sh"),
            worktree_path=str(repo_path),
            job_id=job.id,
            issue_number=job.issue_number,
            pr_number=job.pr_number,
            review_round=job.review_round,
            omx_session_id=omx_session_id,
        )

    def wait(self, tmux_session: str, *, poll_interval: float = 0.5, timeout_seconds: float = 1800) -> None:
        return None
