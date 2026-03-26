from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from dani.git_sync import DevSyncConflictError, GitDevSyncer
from dani.github import GitHubCLI, MergeConflictError
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
        dev_syncer: Any = None,
    ) -> None:
        self.config = config
        self.storage = storage or JsonStorage(config)
        self.github = github or GitHubCLI()
        self.omx_runner = omx_runner or OmxRunner(config.run_dir)
        self.dev_syncer = dev_syncer or GitDevSyncer(config.run_dir)
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
            "ref": event.ref,
            "commit_sha": event.commit_sha,
        })
        repo = self.storage.get_repo(event.repo_full_name)
        if repo is None or not repo.enabled:
            return {"status": "ignored", "reason": "unregistered_repo"}

        signature = parse_signature(event.body or "")
        if signature and event.kind != "pull_request_opened":
            return self._handle_agent_event(event, signature)

        if event.kind == "branch_push":
            return self._queue_dev_sync(repo, event)

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
            return self._handle_review_round_agent_event(event, signature)

        if stage == "implementation" and event.kind == "pull_request_comment":
            return self._handle_implementation_agent_event(event, signature)

        if stage == "merge_conflict_resolution":
            return self._handle_merge_conflict_resolution_agent_event(event, signature)

        if stage == "final_verdict" and signature.get("verdict") == "APPROVE":
            return self._handle_final_verdict_agent_event(event, signature)

        return {"status": "updated", "stage": stage}

    def _handle_review_round_agent_event(self, event: NormalizedEvent, signature: dict[str, str]) -> dict[str, Any]:
        event_key = self._agent_event_key(signature, default_pr=event.number if event.is_pull_request else None)
        if not self.storage.record_processed_event(event_key):
            return {"status": "ignored", "reason": "duplicate_agent_event"}
        review_round = int(signature["round"])
        pr_number = int(signature["pr"])
        issue_number = self._issue_number_for_signature_event(event.repo_full_name, signature, pr_number=pr_number)
        repo = self.storage.get_repo(event.repo_full_name)
        if repo is None:
            return {"status": "ignored", "reason": "missing_repo"}
        pr_metadata = self._pull_request_metadata(event.repo_full_name, pr_number)
        next_job = self._enqueue_job(
            repo,
            stage="implementation",
            issue_number=issue_number,
            pr_number=pr_number,
            review_round=review_round,
            metadata={
                **pr_metadata,
                "title": (pr_metadata.get("title") or event.title or ""),
                "review_comment_body": event.body or "",
                "triggering_review_round": review_round,
            },
        )
        return {"status": "queued", "job_id": next_job.id, "stage": next_job.stage}

    def _handle_implementation_agent_event(self, event: NormalizedEvent, signature: dict[str, str]) -> dict[str, Any]:
        pr_number = int(signature.get("pr") or event.number)
        event_key = self._agent_event_key(signature, default_pr=pr_number)
        if not self.storage.record_processed_event(event_key):
            return {"status": "ignored", "reason": "duplicate_agent_event"}
        repo = self.storage.get_repo(event.repo_full_name)
        if repo is None:
            return {"status": "ignored", "reason": "missing_repo"}
        issue_number = self._issue_number_for_signature_event(event.repo_full_name, signature, pr_number=pr_number)
        latest_review_round = self._latest_review_round(event.repo_full_name, pr_number)
        pr_metadata = self._pull_request_metadata(event.repo_full_name, pr_number)
        if latest_review_round >= self.config.review_rounds:
            verdict_job = self._enqueue_job(
                repo,
                stage="final_verdict",
                issue_number=issue_number,
                pr_number=pr_number,
                metadata={
                    **pr_metadata,
                    "title": (pr_metadata.get("title") or event.title or ""),
                },
            )
            return {"status": "queued", "job_id": verdict_job.id, "stage": verdict_job.stage}

        next_round = max(latest_review_round + 1, 1)
        review_job = self._enqueue_job(
            repo,
            stage="review_round",
            issue_number=issue_number,
            pr_number=pr_number,
            review_round=next_round,
            metadata={
                **pr_metadata,
                "title": (pr_metadata.get("title") or event.title or ""),
            },
        )
        return {"status": "queued", "job_id": review_job.id, "stage": review_job.stage}

    def _handle_merge_conflict_resolution_agent_event(
        self, event: NormalizedEvent, signature: dict[str, str]
    ) -> dict[str, Any]:
        repo = self.storage.get_repo(event.repo_full_name)
        if repo is None:
            return {"status": "ignored", "reason": "missing_repo"}
        pr_number = int(signature["pr"])
        pr_metadata = self._pull_request_metadata(event.repo_full_name, pr_number)
        issue_number = self._issue_number_for_signature_event(event.repo_full_name, signature, pr_number=pr_number)
        if issue_number is None:
            issue_number = self._extract_issue_number(pr_metadata.get("body"))
        verdict_job = self._enqueue_job(
            repo,
            stage="final_verdict",
            issue_number=issue_number,
            pr_number=pr_number,
            metadata={
                **pr_metadata,
                "title": (pr_metadata.get("title") or event.title or ""),
            },
        )
        return {"status": "queued", "job_id": verdict_job.id, "stage": verdict_job.stage}

    def _handle_final_verdict_agent_event(self, event: NormalizedEvent, signature: dict[str, str]) -> dict[str, Any]:
        pr_number = int(signature["pr"])
        try:
            self.github.merge_pull_request(event.repo_full_name, pr_number)
        except MergeConflictError as exc:
            repo = self.storage.get_repo(event.repo_full_name)
            if repo is None:
                return {"status": "ignored", "reason": "missing_repo"}
            pull_request = self.github.get_pull_request(event.repo_full_name, pr_number)
            merge_conflict_job = self._enqueue_job(
                repo,
                stage="merge_conflict_resolution",
                issue_number=self._extract_issue_number(pull_request.get("body")),
                pr_number=pr_number,
                metadata={
                    "title": pull_request.get("title") or event.title or f"PR #{pr_number}",
                    "body": pull_request.get("body") or "",
                    "head_branch": self._branch_ref(pull_request, "head"),
                    "base_branch": self._branch_ref(pull_request, "base"),
                    "conflict_reason": str(exc),
                },
            )
            return {"status": "queued", "job_id": merge_conflict_job.id, "stage": merge_conflict_job.stage}
        return {"status": "merged", "pr_number": pr_number}

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

        if job.stage == "dev_sync":
            self._run_dev_sync_job(repo, job)
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

    def _run_dev_sync_job(self, repo: RepoConfig, job: JobRecord) -> None:
        session = None
        conflict_context = None
        try:
            outcome = self.dev_syncer.sync(repo, job)
            self.storage.update_job(
                job.id, status="completed", metadata={**job.metadata, "sync_status": outcome.status}
            )
        except DevSyncConflictError as exc:
            conflict_context = exc.context
            prompt = render_prompt(
                "dev_sync_conflict",
                {
                    "repo": repo.full_name,
                    "local_path": str(conflict_context.worktree_path),
                    "main_branch": repo.main_branch,
                    "dev_branch": repo.dev_branch,
                    "main_sha": conflict_context.source_sha,
                    "temp_branch": conflict_context.temp_branch,
                    "commit_message": self.dev_syncer.build_commit_message(repo, job),
                },
            )
            session = self.omx_runner.launch(conflict_context.worktree_path, job, prompt)
            self.storage.create_session(session)
            self.storage.update_job(
                job.id,
                status="launched",
                session_id=session.id,
                metadata={
                    **job.metadata,
                    "sync_status": "conflict",
                    "worktree_path": str(conflict_context.worktree_path),
                },
            )
            self.omx_runner.wait(session.runtime_handle)
            self.dev_syncer.verify_remote_sync(conflict_context)
            self._finalize_session(session, status="completed", termination_reason="completed")
            self.storage.update_job(
                job.id,
                status="completed",
                metadata={**job.metadata, "sync_status": "resolved_with_omx"},
            )
        except Exception as exc:
            if session is not None:
                self._finalize_session(session, status="failed", termination_reason=type(exc).__name__)
            metadata = {**job.metadata, "error": str(exc)}
            if conflict_context is not None:
                metadata["worktree_path"] = str(conflict_context.worktree_path)
            self.storage.update_job(job.id, status="failed", metadata=metadata)
        finally:
            if conflict_context is not None:
                self.dev_syncer.cleanup(conflict_context)

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
        issue_context = self._issue_metadata(repo.full_name, issue_number) if issue_number else {}
        issue_title = issue_context.get("title", job.metadata.get("title", f"Issue #{issue_number}"))
        issue_body = issue_context.get("body", job.metadata.get("body", ""))
        pr_snapshot = self._pull_request_metadata(repo.full_name, pr_number) if pr_number else {}
        pr_title = pr_snapshot.get("title", job.metadata.get("title", f"PR #{pr_number}"))
        pr_body = pr_snapshot.get("body", job.metadata.get("body", ""))
        if job.stage == "issue_request":
            return self._build_issue_request_prompt(repo, job, issue_number, issue_title, issue_body)

        if job.stage == "implementation":
            return self._build_implementation_prompt(
                repo, job, issue_number, issue_title, issue_body, pr_number, pr_title, pr_body
            )

        if job.stage == "issue_followup":
            return self._build_issue_followup_prompt(repo, job, issue_number, issue_title, issue_body)

        if job.stage == "review_round":
            return self._build_review_round_prompt(repo, job, issue_number, pr_number, pr_title, pr_body)

        if job.stage == "merge_conflict_resolution":
            return self._build_merge_conflict_resolution_prompt(repo, job, issue_number, pr_number, pr_title, pr_body)

        return self._build_final_verdict_prompt(job, issue_number, pr_number, pr_title, pr_body)

    def _verify_side_effect(self, repo: RepoConfig, job: JobRecord) -> None:
        if job.stage == "issue_request":
            self._verify_issue_request_side_effect(repo, job)
            return
        if job.stage == "issue_followup":
            self._verify_issue_followup_side_effect(repo, job)
            return
        if job.stage == "implementation":
            self._verify_implementation_side_effect(repo, job)
            return
        if job.stage == "review_round":
            self._verify_review_round_side_effect(repo, job)
            return
        if job.stage == "merge_conflict_resolution":
            self._verify_merge_conflict_resolution_side_effect(repo, job)
            return
        if job.stage == "final_verdict":
            self._verify_final_verdict_side_effect(repo, job)

    def _build_issue_request_prompt(
        self,
        repo: RepoConfig,
        job: JobRecord,
        issue_number: int,
        issue_title: str,
        issue_body: str,
    ) -> str:
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

    def _build_implementation_prompt(
        self,
        repo: RepoConfig,
        job: JobRecord,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        pr_number: int,
        pr_title: str,
        pr_body: str,
    ) -> str:
        pr_discussion = self._render_pr_discussion(repo.full_name, pr_number) if pr_number else ""
        pr_context = ""
        if pr_number:
            pr_context = (
                f"Existing PR context:\n"
                f"PR #{pr_number}: {pr_title}\n\n"
                f"Current PR body:\n{pr_body}\n\n"
                f"PR review/comment history to address:\n{pr_discussion or job.metadata.get('review_comment_body', '')}\n"
            )
        signature_fields: dict[str, int | str] = {
            "stage": "implementation",
            "job": job.id,
        }
        if issue_number:
            signature_fields["issue"] = issue_number
        if pr_number:
            signature_fields["pr"] = pr_number
        signature = build_signature(**signature_fields)
        if pr_number:
            signature_instructions = (
                "  - This is an existing PR follow-up.\n"
                "  - Write exactly one PR comment that summarizes the fixes and includes this exact signature:\n"
                f"{signature}\n"
                "  - Post that follow-up with:\n"
                f"    gh pr comment {pr_number} --repo {repo.full_name} --body-file <implementation-update.md>"
            )
        else:
            signature_instructions = (
                "  - If this is the first implementation and no PR exists yet, put this signature in the PR body:\n"
                f"{signature}"
            )
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
                "pr_number": pr_number or "",
                "pr_title": pr_title,
                "pr_body": pr_body,
                "pr_discussion": pr_discussion or job.metadata.get("review_comment_body", ""),
                "pr_context": pr_context,
                "signature": signature,
                "signature_instructions": signature_instructions,
            },
        )

    def _build_issue_followup_prompt(
        self,
        repo: RepoConfig,
        job: JobRecord,
        issue_number: int,
        issue_title: str,
        issue_body: str,
    ) -> str:
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

    def _build_review_round_prompt(
        self,
        repo: RepoConfig,
        job: JobRecord,
        issue_number: int,
        pr_number: int,
        pr_title: str,
        pr_body: str,
    ) -> str:
        signature_fields: dict[str, int | str] = {
            "stage": "review_round",
            "job": job.id,
            "pr": pr_number,
            "round": job.review_round or 1,
        }
        if issue_number:
            signature_fields["issue"] = issue_number
        discussion_parts = []
        if issue_number:
            discussion_parts.append(f"Related issue: #{issue_number}")
        pr_discussion = self._render_pr_discussion(repo.full_name, pr_number)
        if pr_discussion:
            discussion_parts.append(pr_discussion)
        return render_prompt(
            "review_round",
            {
                "repo": repo.full_name,
                "pr_number": pr_number,
                "pr_title": pr_title,
                "pr_body": pr_body,
                "discussion": "\n\n".join(discussion_parts),
                "round_number": job.review_round or 1,
                "signature": build_signature(**signature_fields),
            },
        )

    def _build_merge_conflict_resolution_prompt(
        self,
        repo: RepoConfig,
        job: JobRecord,
        issue_number: int,
        pr_number: int,
        pr_title: str,
        pr_body: str,
    ) -> str:
        return render_prompt(
            "merge_conflict_resolution",
            {
                "repo": repo.full_name,
                "local_path": repo.local_path,
                "issue_number": issue_number,
                "pr_number": pr_number,
                "pr_title": pr_title,
                "pr_body": pr_body,
                "head_branch": job.metadata.get("head_branch", ""),
                "base_branch": job.metadata.get("base_branch", repo.dev_branch),
                "conflict_reason": job.metadata.get("conflict_reason", "Merge conflict detected while merging."),
                "signature": build_signature(stage="merge_conflict_resolution", job=job.id, pr=pr_number),
            },
        )

    def _build_final_verdict_prompt(
        self,
        job: JobRecord,
        issue_number: int,
        pr_number: int,
        pr_title: str,
        pr_body: str,
    ) -> str:
        discussion_parts = []
        if issue_number:
            discussion_parts.append(f"Related issue: #{issue_number}")
        pr_discussion = self._render_pr_discussion(job.repo_full_name, pr_number)
        if pr_discussion:
            discussion_parts.append(pr_discussion)
        return render_prompt(
            "final_verdict",
            {
                "repo": job.repo_full_name,
                "pr_number": pr_number,
                "pr_title": pr_title,
                "pr_body": pr_body,
                "discussion": "\n\n".join(discussion_parts),
                "approve_signature": build_signature(
                    stage="final_verdict", job=job.id, pr=pr_number, verdict="APPROVE"
                ),
                "reject_signature": build_signature(stage="final_verdict", job=job.id, pr=pr_number, verdict="REJECT"),
            },
        )

    def _verify_issue_request_side_effect(self, repo: RepoConfig, job: JobRecord) -> None:
        if self.github.latest_signature_comment(repo.full_name, int(job.issue_number or 0), kind="issue") is None:
            raise RuntimeError("issue-request-comment-missing")

    def _verify_issue_followup_side_effect(self, repo: RepoConfig, job: JobRecord) -> None:
        latest_comment = self.github.latest_signature_comment(repo.full_name, int(job.issue_number or 0), kind="issue")
        if latest_comment is None or latest_comment[1].get("stage") != "issue_followup":
            raise RuntimeError("issue-followup-comment-missing")

    def _verify_implementation_side_effect(self, repo: RepoConfig, job: JobRecord) -> None:
        signature_fields: dict[str, int | str] = {
            "stage": "implementation",
            "job": job.id,
        }
        if job.issue_number:
            signature_fields["issue"] = int(job.issue_number)
        if job.pr_number:
            signature_fields["pr"] = int(job.pr_number)
            signature = build_signature(**signature_fields)
            if not self._has_exact_pr_signature(repo.full_name, int(job.pr_number), signature):
                raise RuntimeError("implementation-comment-missing")
            return
        signature = build_signature(**signature_fields)
        if self.github.find_pr_by_signature(repo.full_name, signature) is None:
            raise RuntimeError("implementation-pr-missing")

    def _verify_review_round_side_effect(self, repo: RepoConfig, job: JobRecord) -> None:
        signature_fields: dict[str, int | str] = {
            "stage": "review_round",
            "job": job.id,
            "pr": int(job.pr_number or 0),
            "round": job.review_round or 1,
        }
        if job.issue_number:
            signature_fields["issue"] = int(job.issue_number)
        signature = build_signature(**signature_fields)
        if not self._has_exact_pr_signature(repo.full_name, int(job.pr_number or 0), signature):
            raise RuntimeError("review-comment-missing")

    def _verify_merge_conflict_resolution_side_effect(self, repo: RepoConfig, job: JobRecord) -> None:
        signature = build_signature(stage="merge_conflict_resolution", job=job.id, pr=int(job.pr_number or 0))
        if not self._has_exact_pr_signature(repo.full_name, int(job.pr_number or 0), signature):
            raise RuntimeError("merge-conflict-comment-missing")

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

    def _branch_ref(self, payload: dict[str, Any], key: str) -> str | None:
        ref_payload = payload.get(key)
        if isinstance(ref_payload, dict):
            ref = ref_payload.get("ref")
            if isinstance(ref, str) and ref:
                return ref
        return None

    def _queue_issue_request(self, repo: RepoConfig, event: NormalizedEvent) -> dict[str, Any]:
        job = self._enqueue_job(
            repo,
            stage="issue_request",
            issue_number=event.number,
            metadata={"title": event.title or "", "body": event.body or ""},
        )
        return {"status": "queued", "job_id": job.id, "stage": job.stage}

    def _queue_dev_sync(self, repo: RepoConfig, event: NormalizedEvent) -> dict[str, Any]:
        if event.ref != f"refs/heads/{repo.main_branch}":
            return {"status": "ignored", "reason": "non_main_push"}
        if not event.commit_sha:
            return {"status": "ignored", "reason": "missing_commit_sha"}
        if self._has_existing_dev_sync_job(repo.full_name, event.commit_sha):
            return {"status": "ignored", "reason": "duplicate_dev_sync"}
        job = self._enqueue_job(
            repo,
            stage="dev_sync",
            metadata={"main_sha": event.commit_sha, "ref": event.ref},
        )
        return {"status": "queued", "job_id": job.id, "stage": job.stage}

    def _has_existing_dev_sync_job(self, repo_full_name: str, main_sha: str) -> bool:
        for job in self.storage.find_jobs(repo_full_name=repo_full_name, stage="dev_sync"):
            if job.metadata.get("main_sha") != main_sha:
                continue
            if job.status in {"queued", "launched", "completed"}:
                return True
        return False

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

    def _agent_event_key(self, signature: dict[str, str], *, default_pr: int | None = None) -> str:
        fields = [("stage", signature.get("stage", ""))]
        for key in ("pr", "round", "job", "verdict"):
            value = signature.get(key)
            if key == "pr" and not value and default_pr is not None:
                value = str(default_pr)
            if value:
                fields.append((key, value))
        return ";".join(f"{key}={value}" for key, value in fields)

    def _latest_review_round(self, repo_full_name: str, pr_number: int) -> int:
        rounds = [
            int(job.review_round or 0)
            for job in self.storage.find_jobs(repo_full_name=repo_full_name, stage="review_round", pr_number=pr_number)
        ]
        return max(rounds, default=0)

    def _issue_number_for_signature_event(
        self,
        repo_full_name: str,
        signature: dict[str, str],
        *,
        pr_number: int,
    ) -> int | None:
        issue_value = signature.get("issue")
        if issue_value:
            return int(issue_value)
        for job in reversed(self.storage.list_jobs()):
            if job.repo_full_name != repo_full_name or job.pr_number != pr_number or job.issue_number is None:
                continue
            return int(job.issue_number)
        return None

    def _issue_metadata(self, repo_full_name: str, issue_number: int) -> dict[str, str]:
        for job in reversed(self.storage.list_jobs()):
            if job.repo_full_name != repo_full_name or job.issue_number != issue_number:
                continue
            title = job.metadata.get("issue_title") or job.metadata.get("title")
            body = job.metadata.get("issue_body") or job.metadata.get("body")
            if isinstance(title, str) or isinstance(body, str):
                return {
                    "title": title if isinstance(title, str) else f"Issue #{issue_number}",
                    "body": body if isinstance(body, str) else "",
                }
        return {}

    def _pull_request_metadata(self, repo_full_name: str, pr_number: int) -> dict[str, str]:
        for pull_request in self.github.list_pull_requests(repo_full_name):
            if int(pull_request.get("number", 0)) != pr_number:
                continue
            return {
                "title": str(pull_request.get("title") or f"PR #{pr_number}"),
                "body": str(pull_request.get("body") or ""),
            }
        return {}

    def _render_pr_discussion(self, repo_full_name: str, pr_number: int, *, limit: int = 8) -> str:
        comments = self.github.pr_comments(repo_full_name, pr_number)
        rendered: list[str] = []
        for comment in comments[-limit:]:
            body = str(comment.get("body") or "").strip()
            if not body:
                continue
            author = comment.get("user", {}).get("login") or comment.get("author", {}).get("login") or "unknown"
            rendered.append(f"[{author}]\n{body}")
        return "\n\n".join(rendered)
