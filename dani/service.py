from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from dani.github import GitHubCLI
from dani.models import DaniConfig, JobRecord, NormalizedEvent, RepoConfig, SessionRecord, utc_now
from dani.omx_runner import OmxRunner
from dani.prompts import render_prompt
from dani.queue import RepoQueueManager
from dani.signatures import build_signature, parse_signature
from dani.storage import JsonStorage

ISSUE_REF_PATTERN = re.compile(r"#(?P<number>\d+)")


class DaniService:
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
            latest_signature = self.github.latest_signature_comment(repo_full_name, issue["number"], kind="issue")
            if latest_signature is not None and latest_signature[1].get("stage") == "issue_request":
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
            return self._queue_issue_request(repo, event)

        if event.kind == "issue_comment" and self._is_approve_comment(event.body):
            return self._queue_implementation(repo, event)

        if event.kind == "issue_comment":
            return self._queue_issue_followup(repo, event)

        if event.kind == "pull_request_opened":
            return self._queue_pull_request_review(repo, event, signature)

        return {"status": "ignored", "reason": "unsupported_event"}

    def _handle_agent_event(self, event: NormalizedEvent, signature: dict[str, str]) -> dict[str, Any]:
        stage = signature.get("stage")
        if stage == "review_round":
            event_key = self._agent_event_key(signature)
            if not self.storage.record_processed_event(event_key):
                return {"status": "ignored", "reason": "duplicate_agent_event"}
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
            if job.stage == "issue_followup":
                session = self.omx_runner.resume(Path(repo.local_path), job, prompt, self._omx_session_id_for(job))
            else:
                session = self.omx_runner.launch(Path(repo.local_path), job, prompt)
            self.storage.create_session(session)
            self.storage.update_job(job.id, status="launched", session_id=session.id)
            self.omx_runner.wait(session.runtime_handle)
            self._verify_side_effect(repo, job)
            self._finalize_session(session, status="completed", termination_reason="completed")
            self.storage.update_job(job.id, status="completed")
        except Exception as exc:
            if session is not None:
                self._finalize_session(session, status="failed", termination_reason=type(exc).__name__)
            self.storage.update_job(job.id, status="failed", metadata={**job.metadata, "error": str(exc)})

    def _finalize_session(self, session: SessionRecord, *, status: str, termination_reason: str) -> None:
        try:
            self.omx_runner.close_session(session.runtime_handle)
        finally:
            self.storage.update_session(
                session.id,
                status=status,
                ended_at=utc_now(),
                termination_reason=termination_reason,
            )

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
                },
            )

        if job.stage == "issue_followup":
            return render_prompt(
                "issue_followup",
                {
                    "repo": repo.full_name,
                    "local_path": repo.local_path,
                    "issue_number": issue_number,
                    "issue_title": issue_title,
                    "issue_body": issue_body,
                    "comment_body": job.metadata.get("comment_body", ""),
                    "signature": build_signature(stage="issue_followup", job=job.id, issue=issue_number),
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
            },
        )

    def _verify_side_effect(self, repo: RepoConfig, job: JobRecord) -> None:
        if job.stage == "issue_request":
            if self.github.latest_signature_comment(repo.full_name, int(job.issue_number or 0), kind="issue") is None:
                raise RuntimeError("issue-request-comment-missing")
            return
        if job.stage == "issue_followup":
            latest_comment = self.github.latest_signature_comment(
                repo.full_name, int(job.issue_number or 0), kind="issue"
            )
            if latest_comment is None or latest_comment[1].get("stage") != "issue_followup":
                raise RuntimeError("issue-followup-comment-missing")
            return
        if job.stage == "implementation":
            signature = build_signature(stage="implementation", job=job.id, issue=int(job.issue_number or 0))
            if self.github.find_pr_by_signature(repo.full_name, signature) is None:
                raise RuntimeError("implementation-pr-missing")
            return
        if job.stage == "review_round":
            self._verify_review_round_side_effect(repo, job)
            return
        if job.stage == "final_verdict":
            self._verify_final_verdict_side_effect(repo, job)

    def _verify_review_round_side_effect(self, repo: RepoConfig, job: JobRecord) -> None:
        signature = build_signature(
            stage="review_round",
            job=job.id,
            pr=int(job.pr_number or 0),
            round=job.review_round or 1,
        )
        if not self._has_exact_pr_signature(repo.full_name, int(job.pr_number or 0), signature):
            raise RuntimeError("review-comment-missing")

    def _verify_final_verdict_side_effect(self, repo: RepoConfig, job: JobRecord) -> None:
        approve_signature = build_signature(
            stage="final_verdict",
            job=job.id,
            pr=int(job.pr_number or 0),
            verdict="APPROVE",
        )
        reject_signature = build_signature(
            stage="final_verdict",
            job=job.id,
            pr=int(job.pr_number or 0),
            verdict="REJECT",
        )
        if not (
            self._has_exact_pr_signature(repo.full_name, int(job.pr_number or 0), approve_signature)
            or self._has_exact_pr_signature(repo.full_name, int(job.pr_number or 0), reject_signature)
        ):
            raise RuntimeError("final-verdict-comment-missing")

    def _has_exact_pr_signature(self, repo_full_name: str, pr_number: int, signature: str) -> bool:
        return bool(
            self.github.find_comments_by_signature(repo_full_name, pr_number, kind="pr", signature_fragment=signature)
        )

    def _is_approve_comment(self, body: str | None) -> bool:
        return bool(body and "/approve" in body.lower())

    def _extract_issue_number(self, body: str | None) -> int | None:
        if not body:
            return None
        match = ISSUE_REF_PATTERN.search(body)
        if match is None:
            return None
        return int(match.group("number"))

    def _queue_issue_request(self, repo: RepoConfig, event: NormalizedEvent) -> dict[str, Any]:
        job = self._enqueue_job(
            repo,
            stage="issue_request",
            issue_number=event.number,
            metadata={"title": event.title or "", "body": event.body or ""},
        )
        return {"status": "queued", "job_id": job.id, "stage": job.stage}

    def _queue_implementation(self, repo: RepoConfig, event: NormalizedEvent) -> dict[str, Any]:
        job = self._enqueue_job(
            repo,
            stage="implementation",
            issue_number=event.number,
            metadata={"title": event.title or "", "body": event.payload.get("issue", {}).get("body", "")},
        )
        return {"status": "queued", "job_id": job.id, "stage": job.stage}

    def _queue_issue_followup(self, repo: RepoConfig, event: NormalizedEvent) -> dict[str, Any]:
        session = self._latest_resumable_session(
            repo_full_name=event.repo_full_name,
            stage="issue_request",
            issue_number=event.number,
        )
        if session is None or session.omx_session_id is None:
            return {"status": "ignored", "reason": "missing_issue_session"}
        job = self._enqueue_job(
            repo,
            stage="issue_followup",
            issue_number=event.number,
            metadata={
                "title": event.title or "",
                "body": event.payload.get("issue", {}).get("body", ""),
                "comment_body": event.body or "",
                "omx_session_id": session.omx_session_id,
            },
        )
        return {"status": "queued", "job_id": job.id, "stage": job.stage}

    def _omx_session_id_for(self, job: JobRecord) -> str:
        omx_session_id = job.metadata.get("omx_session_id")
        if isinstance(omx_session_id, str) and omx_session_id:
            return omx_session_id
        msg = "missing-omx-session-id"
        raise RuntimeError(msg)

    def _queue_pull_request_review(
        self, repo: RepoConfig, event: NormalizedEvent, signature: dict[str, str] | None
    ) -> dict[str, Any]:
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

    def _latest_resumable_session(
        self,
        *,
        repo_full_name: str,
        stage: str,
        issue_number: int | None = None,
        pr_number: int | None = None,
    ) -> SessionRecord | None:
        return self.storage.find_latest_session(
            repo_full_name=repo_full_name,
            stage=stage,
            issue_number=issue_number,
            pr_number=pr_number,
            require_omx_session_id=True,
        )

    def _agent_event_key(self, signature: dict[str, str]) -> str:
        fields = [("stage", signature.get("stage", ""))]
        for key in ("pr", "round", "job", "verdict"):
            value = signature.get(key)
            if value:
                fields.append((key, value))
        return ";".join(f"{key}={value}" for key, value in fields)
