from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path
from typing import Any

from dani.github import GitHubCLI
from dani.models import DaniConfig, JobRecord, NormalizedEvent, RepoConfig
from dani.omx_runner import OmxRunner
from dani.prompts import render_prompt
from dani.queue import RepoQueueManager
from dani.signatures import build_signature, parse_signature
from dani.storage import JsonStorage

ISSUE_REF_PATTERN = re.compile(r"#(?P<number>\d+)")


class DaniService:
    @staticmethod
    def _github_helper_command() -> str:
        python_executable = shlex.quote(sys.executable)
        helper_script = shlex.quote(str(Path(__file__).with_name("github_helper.py")))
        return f"{python_executable} {helper_script}"

    def __init__(
        self,
        config: DaniConfig,
        storage: JsonStorage | None = None,
        github: Any = None,
        omx_runner: Any = None,
    ) -> None:
        self.config = config
        self.storage = storage or JsonStorage(config)
        self.github = github or GitHubCLI()
        self.omx_runner = omx_runner or OmxRunner(config.run_dir)
        self.queue_manager = RepoQueueManager(self._run_job)

    def register_repo(
        self, full_name: str, local_path: str, main_branch: str = "main", dev_branch: str = "dev"
    ) -> RepoConfig:
        repo = RepoConfig(full_name=full_name, local_path=local_path, main_branch=main_branch, dev_branch=dev_branch)
        self.storage.register_repo(repo)
        return repo

    def bootstrap_repo(self, repo_full_name: str) -> int:
        issues = self.github.list_open_issues(repo_full_name)
        count = 0
        for issue in issues:
            if "pull_request" in issue:
                continue
            event = NormalizedEvent(
                kind="issue_opened",
                repo_full_name=repo_full_name,
                action="bootstrap",
                number=issue["number"],
                actor_login="bootstrap",
                payload={"bootstrap": True, "issue": issue},
                body=issue.get("body"),
                title=issue.get("title"),
            )
            self.handle_event(event)
            count += 1
        return count

    def wait_for_idle(self) -> None:
        self.queue_manager.join_all()

    def state_snapshot(self) -> dict[str, Any]:
        return self.storage.snapshot()

    def handle_event(self, event: NormalizedEvent) -> dict[str, Any]:
        self.storage.append_event({
            "repo_full_name": event.repo_full_name,
            "kind": event.kind,
            "action": event.action,
            "number": event.number,
            "actor_login": event.actor_login,
            "body": event.body,
            "title": event.title,
            "base_branch": event.base_branch,
            "head_branch": event.head_branch,
        })
        repo = self.storage.get_repo(event.repo_full_name)
        if repo is None or not repo.enabled:
            return {"status": "ignored", "reason": "unregistered_repo"}

        signature = parse_signature(event.body or "")
        if signature and event.kind != "pull_request_opened":
            return self._handle_agent_event(event, signature)

        if event.kind == "issue_opened":
            job = self._enqueue_job(
                repo,
                stage="issue_request",
                issue_number=event.number,
                metadata={"title": event.title or "", "body": event.body or ""},
            )
            return {"status": "queued", "job_id": job.id, "stage": job.stage}

        if event.kind == "issue_comment" and self._is_approve_comment(event.body):
            job = self._enqueue_job(
                repo,
                stage="implementation",
                issue_number=event.number,
                metadata={"title": event.title or "", "body": event.payload.get("issue", {}).get("body", "")},
            )
            return {"status": "queued", "job_id": job.id, "stage": job.stage}

        if event.kind == "pull_request_opened":
            if event.base_branch == repo.main_branch:
                return {"status": "ignored", "reason": "release_loop_excluded"}
            issue_number = None
            if signature and signature.get("issue"):
                issue_number = int(signature["issue"])
                if signature.get("job") and self.storage.get_job(signature["job"]) is not None:
                    self.storage.update_job(signature["job"], status="completed", pr_number=event.number)
            if issue_number is None:
                issue_number = self._extract_issue_number(event.body)
            job = self._enqueue_job(
                repo,
                stage="review_round",
                issue_number=issue_number,
                pr_number=event.number,
                review_round=1,
                metadata={"title": event.title or "", "body": event.body or ""},
            )
            return {"status": "queued", "job_id": job.id, "stage": job.stage}

        return {"status": "ignored", "reason": "unsupported_event"}

    def _handle_agent_event(self, event: NormalizedEvent, signature: dict[str, str]) -> dict[str, Any]:
        stage = signature.get("stage")
        if stage == "review_round":
            review_round = int(signature["round"])
            pr_number = int(signature["pr"])
            if review_round < self.config.review_rounds:
                repo = self.storage.get_repo(event.repo_full_name)
                if repo is None:
                    return {"status": "ignored", "reason": "missing_repo"}
                next_job = self._enqueue_job(
                    repo,
                    stage="review_round",
                    pr_number=pr_number,
                    review_round=review_round + 1,
                    metadata={"title": event.title or "", "body": event.body or ""},
                )
                return {"status": "queued", "job_id": next_job.id, "stage": next_job.stage}

            repo = self.storage.get_repo(event.repo_full_name)
            if repo is None:
                return {"status": "ignored", "reason": "missing_repo"}
            verdict_job = self._enqueue_job(
                repo,
                stage="final_verdict",
                pr_number=pr_number,
                metadata={"title": event.title or "", "body": event.body or ""},
            )
            return {"status": "queued", "job_id": verdict_job.id, "stage": verdict_job.stage}

        if stage == "final_verdict" and signature.get("verdict") == "APPROVE":
            self.github.merge_pull_request(event.repo_full_name, int(signature["pr"]))
            return {"status": "merged", "pr_number": int(signature["pr"])}

        return {"status": "updated", "stage": stage}

    def _enqueue_job(
        self,
        repo: RepoConfig,
        *,
        stage: str,
        issue_number: int | None = None,
        pr_number: int | None = None,
        review_round: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> JobRecord:
        job = JobRecord(
            repo_full_name=repo.full_name,
            stage=stage,
            issue_number=issue_number,
            pr_number=pr_number,
            review_round=review_round,
            metadata=metadata or {},
        )
        self.storage.create_job(job)
        self.queue_manager.submit(job)
        return job

    def _run_job(self, job: JobRecord) -> None:
        repo = self.storage.get_repo(job.repo_full_name)
        if repo is None:
            self.storage.update_job(job.id, status="failed", metadata={**job.metadata, "error": "missing repo"})
            return

        session = None
        try:
            prompt = self._build_prompt(repo, job)
            session = self.omx_runner.launch(Path(repo.local_path), job, prompt)
            self.storage.create_session(session)
            self.storage.update_job(job.id, status="launched", session_id=session.id)
            self.omx_runner.wait(session.tmux_session)
            self.storage.update_session(session.id, status="completed")
            self._verify_side_effect(repo, job)
            self.storage.update_job(job.id, status="completed")
        except Exception as exc:
            if session is not None:
                self.storage.update_session(session.id, status="failed")
            self.storage.update_job(job.id, status="failed", metadata={**job.metadata, "error": str(exc)})

    def _build_prompt(self, repo: RepoConfig, job: JobRecord) -> str:
        issue_number = job.issue_number or 0
        pr_number = job.pr_number or 0
        issue_title = job.metadata.get("title", f"Issue #{issue_number}")
        pr_title = job.metadata.get("title", f"PR #{pr_number}")
        issue_body = job.metadata.get("body", "")
        pr_body = job.metadata.get("body", "")
        if job.stage == "issue_request":
            return render_prompt(
                "issue_request",
                {
                    "repo": repo.full_name,
                    "local_path": repo.local_path,
                    "issue_number": issue_number,
                    "issue_title": issue_title,
                    "issue_body": issue_body,
                    "signature": build_signature(stage="issue_request", job=job.id, issue=issue_number),
                    "github_helper": self._github_helper_command(),
                },
            )

        if job.stage == "implementation":
            return render_prompt(
                "implementation",
                {
                    "repo": repo.full_name,
                    "local_path": repo.local_path,
                    "issue_number": issue_number,
                    "issue_title": issue_title,
                    "issue_body": issue_body,
                    "discussion": f"Issue #{issue_number} implementation request",
                    "dev_branch": repo.dev_branch,
                    "signature": build_signature(stage="implementation", job=job.id, issue=issue_number),
                    "github_helper": self._github_helper_command(),
                },
            )

        if job.stage == "review_round":
            return render_prompt(
                "review_round",
                {
                    "repo": repo.full_name,
                    "pr_number": pr_number,
                    "pr_title": pr_title,
                    "pr_body": pr_body,
                    "discussion": f"Related issue: #{issue_number}" if issue_number else "",
                    "round_number": job.review_round or 1,
                    "signature": build_signature(
                        stage="review_round", job=job.id, pr=pr_number, round=job.review_round or 1
                    ),
                    "github_helper": self._github_helper_command(),
                },
            )

        return render_prompt(
            "final_verdict",
            {
                "repo": repo.full_name,
                "pr_number": pr_number,
                "pr_title": pr_title,
                "pr_body": pr_body,
                "discussion": f"Related issue: #{issue_number}" if issue_number else "",
                "approve_signature": build_signature(
                    stage="final_verdict", job=job.id, pr=pr_number, verdict="APPROVE"
                ),
                "reject_signature": build_signature(stage="final_verdict", job=job.id, pr=pr_number, verdict="REJECT"),
                "github_helper": self._github_helper_command(),
            },
        )

    def _verify_side_effect(self, repo: RepoConfig, job: JobRecord) -> None:
        if job.stage == "issue_request":
            if self.github.latest_signature_comment(repo.full_name, int(job.issue_number or 0), kind="issue") is None:
                raise RuntimeError("issue-request-comment-missing")
            return
        if job.stage == "implementation":
            signature = build_signature(stage="implementation", job=job.id, issue=int(job.issue_number or 0))
            if self.github.find_pr_by_signature(repo.full_name, signature) is None:
                raise RuntimeError("implementation-pr-missing")
            return
        if job.stage == "review_round":
            if self.github.latest_signature_comment(repo.full_name, int(job.pr_number or 0), kind="pr") is None:
                raise RuntimeError("review-comment-missing")
            return
        if (
            job.stage == "final_verdict"
            and self.github.latest_signature_comment(repo.full_name, int(job.pr_number or 0), kind="pr") is None
        ):
            raise RuntimeError("final-verdict-comment-missing")

    def _is_approve_comment(self, body: str | None) -> bool:
        return bool(body and "/approve" in body.lower())

    def _extract_issue_number(self, body: str | None) -> int | None:
        if not body:
            return None
        match = ISSUE_REF_PATTERN.search(body)
        if match is None:
            return None
        return int(match.group("number"))
