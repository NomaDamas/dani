from __future__ import annotations

import contextlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dani.agent_runner import AgentRunner, build_agent_runner, normalize_runtime
from dani.errors import (
    OPENCODE_SESSION_MISSING_PATTERNS,
    ROLLOUT_MISSING_PATTERNS,
    ClaudeUsageLimitError,
    RolloutMissingError,
    TransientCapacityError,
)
from dani.git_sync import DevSyncConflictError, GitDevSyncer
from dani.github import GitHubCLI, MergeConflictError
from dani.models import (
    RUNTIME_OMO,
    RUNTIME_OMX,
    DaniConfig,
    JobRecord,
    NormalizedEvent,
    RepoConfig,
    SessionRecord,
    effective_session_runtime,
    utc_now,
)
from dani.prompts import NON_INTERACTIVE_GUARD, ensure_non_interactive_guard, render_prompt, split_non_interactive_guard
from dani.queue import RepoQueueManager
from dani.session_bridge import BridgeContext, OmoSessionBridge
from dani.signatures import build_signature, is_opt_out_comment, parse_signature
from dani.storage import JsonStorage

ISSUE_REF_PATTERN = re.compile(r"#(?P<number>\d+)")
MIN_EXTERNAL_CONTRIBUTOR_ACCOUNT_AGE = timedelta(days=365)
INELIGIBLE_EXTERNAL_PR_COMMENT = (
    "Thanks for your interest in contributing. This pull request has been closed automatically because this "
    "repository only accepts pull requests from GitHub accounts that are at least one year old. If you would like "
    "to request this change or feature, please open an issue instead so maintainers can review it."
)

RETARGET_REQUEST_STAGE = "retarget_request"
ISSUE_REQUEST_RECOVERY_STAGE = "issue_request_recovery"
ISSUE_FOLLOWUP_RECOVERY_STAGE = "issue_followup_recovery"
ISSUE_COMMENT_RECOVERY_STAGES = {
    ISSUE_REQUEST_RECOVERY_STAGE: "issue_request",
    ISSUE_FOLLOWUP_RECOVERY_STAGE: "issue_followup",
}
ISSUE_COMMENT_MISSING_ERRORS = {
    "issue-request-comment-missing",
    "issue-followup-comment-missing",
}
MAX_COMMENT_RECOVERY_ATTEMPTS = 1

RETRY_BACKOFF_SECONDS: list[int] = [60, 180, 600]

logger = logging.getLogger(__name__)


class DaniService:
    def __init__(
        self,
        config: DaniConfig,
        storage: JsonStorage | None = None,
        github: Any = None,
        omx_runner: AgentRunner | None = None,
        dev_syncer: Any = None,
        runtime_runners: dict[str, AgentRunner] | None = None,
        session_bridge: OmoSessionBridge | None = None,
    ) -> None:
        self.config = config
        self.storage = storage or JsonStorage(config)
        self.github = github or GitHubCLI()
        preferred_runtime = normalize_runtime(config.agent_runtime)
        self.omx_runner: AgentRunner = omx_runner or build_agent_runner(preferred_runtime, config.run_dir)
        self._runtime_runners: dict[str, AgentRunner] = {preferred_runtime: self.omx_runner}
        if runtime_runners:
            self._runtime_runners.update({normalize_runtime(name): runner for name, runner in runtime_runners.items()})
        self.dev_syncer = dev_syncer or GitDevSyncer(config.run_dir)
        self.session_bridge = session_bridge or OmoSessionBridge()
        self.queue_manager = RepoQueueManager(self._run_job)
        self._rehydrate_pending_jobs()

    def _rehydrate_pending_jobs(self) -> None:
        """Submit durable pending jobs that survived a Dani process restart."""
        for job in self.storage.list_jobs():
            if job.status in {"queued", "retrying"}:
                self.queue_manager.submit(job)
                continue
            if job.status == "launched":
                repo = self.storage.get_repo(job.repo_full_name)
                if repo is not None:
                    with contextlib.suppress(Exception):
                        self._verify_side_effect(repo, job)
                        if job.stage in ISSUE_COMMENT_RECOVERY_STAGES:
                            self._complete_comment_recovery(job)
                        self.storage.update_job(
                            job.id,
                            status="completed",
                            metadata={
                                **job.metadata,
                                "recovered_from_status": "launched",
                                "recovered_session_id": job.session_id,
                                "recovered_at": utc_now(),
                                "note": "side_effect_already_posted",
                            },
                        )
                        continue
                recovered = self.storage.update_job(
                    job.id,
                    status="queued",
                    metadata={
                        **job.metadata,
                        "recovered_from_status": "launched",
                        "recovered_session_id": job.session_id,
                        "recovered_at": utc_now(),
                    },
                )
                self.queue_manager.submit(recovered)

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

    def restart_issue(self, repo_full_name: str, issue_number: int) -> JobRecord:
        repo = self.storage.get_repo(repo_full_name)
        if repo is None:
            msg = f"missing_repo: {repo_full_name}"
            raise RuntimeError(msg)

        for job in self.storage.find_jobs(repo_full_name=repo_full_name, issue_number=issue_number):
            self.storage.update_job(job.id, status="superseded")

        issue_metadata = self._issue_metadata(repo_full_name, issue_number)
        return self._enqueue_job(
            repo,
            stage="issue_request",
            issue_number=issue_number,
            metadata={
                "title": issue_metadata.get("title", f"Issue #{issue_number}"),
                "body": issue_metadata.get("body", ""),
            },
        )

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
            "delivery_id": event.delivery_id,
            "ref": event.ref,
            "commit_sha": event.commit_sha,
        })
        repo = self.storage.get_repo(event.repo_full_name)
        if repo is None or not repo.enabled:
            return {"status": "ignored", "reason": "unregistered_repo"}

        if event.kind in {"issue_comment", "pull_request_comment"} and is_opt_out_comment(event.body):
            return {"status": "ignored", "reason": "comment_opt_out"}

        signature = parse_signature(event.body or "")
        if signature and event.kind != "pull_request_opened":
            return self._handle_agent_event(event, signature)

        if event.kind == "branch_push":
            return self._queue_dev_sync(repo, event)

        if event.kind == "pull_request_closed":
            return self._handle_pull_request_closed(repo, event)

        if event.kind == "issue_opened":
            return self._dispatch_issue_opened(repo, event)

        if event.kind == "issue_comment" and self._is_approve_comment(event.body):
            return self._dispatch_approve_comment(repo, event)

        if event.kind == "issue_comment":
            return self._dispatch_issue_followup_comment(repo, event)

        if event.kind == "pull_request_opened":
            return self._dispatch_pull_request_opened(repo, event, signature)

        return {"status": "ignored", "reason": "unsupported_event"}

    def _dispatch_issue_opened(self, repo: RepoConfig, event: NormalizedEvent) -> dict[str, Any]:
        if event.action == "reopened":
            return {"status": "ignored", "reason": "issue_reopened_no_op"}
        if event.issue_state == "closed":
            return {"status": "ignored", "reason": "issue_closed"}
        if self.storage.is_terminal_issue(repo.full_name, event.number):
            return {"status": "ignored", "reason": "issue_terminal"}
        return self._queue_issue_request(repo, event)

    def _dispatch_approve_comment(self, repo: RepoConfig, event: NormalizedEvent) -> dict[str, Any]:
        if event.issue_state == "closed":
            return {"status": "ignored", "reason": "issue_closed"}
        if self.storage.is_terminal_issue(repo.full_name, event.number):
            return {"status": "ignored", "reason": "issue_terminal"}
        if not self._is_authorized_approver(repo, event):
            self._react_unauthorized_approve(repo, event)
            return {"status": "ignored", "reason": "approver_not_authorized"}
        if self._has_existing_implementation_job(repo.full_name, event.number):
            return {"status": "ignored", "reason": "duplicate_implementation"}
        return self._queue_implementation(repo, event)

    _TRUSTED_APPROVE_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER"})

    def _is_authorized_approver(self, repo: RepoConfig, event: NormalizedEvent) -> bool:
        owner_login = repo.full_name.split("/", 1)[0]
        actor = event.actor_login or ""
        if not actor:
            return False
        if actor.casefold() == owner_login.casefold():
            return True
        comment = event.payload.get("comment") if isinstance(event.payload, dict) else None
        author_association = ""
        if isinstance(comment, dict):
            author_association = str(comment.get("author_association") or "").upper()
        if author_association in self._TRUSTED_APPROVE_AUTHOR_ASSOCIATIONS:
            return True
        try:
            return bool(self.github.is_org_member(owner_login, actor))
        except Exception:
            logger.warning(
                "approve_owner_check_failed",
                extra={
                    "repo_full_name": repo.full_name,
                    "owner_login": owner_login,
                    "actor_login": actor,
                },
                exc_info=True,
            )
            return False

    def _react_unauthorized_approve(self, repo: RepoConfig, event: NormalizedEvent) -> None:
        comment = event.payload.get("comment") if isinstance(event.payload, dict) else None
        comment_id = comment.get("id") if isinstance(comment, dict) else None
        if not isinstance(comment_id, int):
            return
        try:
            self.github.add_issue_comment_reaction(repo.full_name, event.number, comment_id, "-1")
        except Exception:
            logger.warning(
                "approve_unauthorized_reaction_failed",
                extra={
                    "repo_full_name": repo.full_name,
                    "issue_number": event.number,
                    "actor_login": event.actor_login,
                    "comment_id": comment_id,
                },
                exc_info=True,
            )

    def _dispatch_issue_followup_comment(self, repo: RepoConfig, event: NormalizedEvent) -> dict[str, Any]:
        if event.issue_state == "closed":
            return {"status": "ignored", "reason": "issue_closed"}
        if self.storage.is_terminal_issue(repo.full_name, event.number):
            return {"status": "ignored", "reason": "issue_terminal"}
        if self._is_dani_self_authored(event):
            return {"status": "ignored", "reason": "self_authored_comment"}
        if self._completed_followup_count(repo.full_name, event.number) >= self.config.max_issue_followups:
            return {"status": "ignored", "reason": "max_followups_reached"}
        return self._queue_issue_followup(repo, event)

    def _dispatch_pull_request_opened(
        self, repo: RepoConfig, event: NormalizedEvent, signature: dict[str, str] | None
    ) -> dict[str, Any]:
        if event.pr_merged is True:
            return {"status": "ignored", "reason": "pr_merged"}
        if event.pr_state == "closed":
            return {"status": "ignored", "reason": "pr_not_open"}
        if self.storage.is_terminal_pr(repo.full_name, event.number):
            return {"status": "ignored", "reason": "pr_terminal"}
        return self._queue_pull_request_review(repo, event, signature)

    def _handle_agent_event(self, event: NormalizedEvent, signature: dict[str, str]) -> dict[str, Any]:
        stage = signature.get("stage")
        if stage == "review_round":
            return self._handle_review_round_event(event, signature)

        if stage == "implementation" and event.kind == "pull_request_comment":
            return self._handle_implementation_agent_event(event, signature)

        if stage == "merge_conflict_resolution":
            return self._handle_merge_conflict_resolution_agent_event(event, signature)

        if stage == "final_verdict" and signature.get("verdict") == "APPROVE":
            return self._handle_final_verdict_agent_event(event, signature)

        if stage == RETARGET_REQUEST_STAGE:
            return {"status": "ignored", "reason": "retarget_request_no_action"}

        return {"status": "updated", "stage": stage}

    def _handle_implementation_agent_event(self, event: NormalizedEvent, signature: dict[str, str]) -> dict[str, Any]:
        pr_number = int(signature.get("pr") or event.number)
        event_key = self._agent_event_key(signature, default_pr=pr_number)
        if not self.storage.record_processed_event(event_key):
            return {"status": "ignored", "reason": "duplicate_agent_event"}
        repo = self.storage.get_repo(event.repo_full_name)
        if repo is None:
            return {"status": "ignored", "reason": "missing_repo"}
        if not self._is_pr_open(event.repo_full_name, pr_number):
            return {"status": "ignored", "reason": "pr_not_open"}
        issue_number = self._issue_number_for_signature_event(event.repo_full_name, signature, pr_number=pr_number)
        if issue_number is None:
            return {"status": "ignored", "reason": "untracked_pr"}
        latest_review_round = self._latest_review_round(event.repo_full_name, pr_number)
        pr_metadata = self._pull_request_metadata(event.repo_full_name, pr_number)
        source_job = self.storage.get_job(signature.get("job", ""))
        lineage_metadata = self._automation_lineage_metadata(source_job)
        if latest_review_round >= self.config.review_rounds:
            verdict_job = self._enqueue_job(
                repo,
                stage="final_verdict",
                issue_number=issue_number,
                pr_number=pr_number,
                metadata={
                    **pr_metadata,
                    **lineage_metadata,
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
                **lineage_metadata,
                "title": (pr_metadata.get("title") or event.title or ""),
            },
        )
        return {"status": "queued", "job_id": review_job.id, "stage": review_job.stage}

    def _handle_merge_conflict_resolution_agent_event(
        self, event: NormalizedEvent, signature: dict[str, str]
    ) -> dict[str, Any]:
        pr_number = int(signature["pr"])
        event_key = self._agent_event_key(signature, default_pr=pr_number)
        if not self.storage.record_processed_event(event_key):
            return {"status": "ignored", "reason": "duplicate_agent_event"}
        repo = self.storage.get_repo(event.repo_full_name)
        if repo is None:
            return {"status": "ignored", "reason": "missing_repo"}
        if not self._is_pr_open(event.repo_full_name, pr_number):
            return {"status": "ignored", "reason": "pr_not_open"}
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

    def _automation_lineage_metadata(self, source_job: JobRecord | None) -> dict[str, Any]:
        if source_job is None:
            return {}
        keys = ("external_contribution", "pr_author_login", "pr_author_association")
        return {key: source_job.metadata[key] for key in keys if key in source_job.metadata}

    def _external_pr_metadata(self, event: NormalizedEvent) -> dict[str, Any]:
        pull_request = event.payload.get("pull_request") or {}
        user = pull_request.get("user") or {}
        metadata: dict[str, Any] = {"external_contribution": True}
        if isinstance(user, dict) and user.get("login"):
            metadata["pr_author_login"] = str(user["login"])
        author_association = pull_request.get("author_association")
        if author_association:
            metadata["pr_author_association"] = str(author_association)
        return metadata

    def _pull_request_author_is_repo_owner(self, repo_full_name: str, pull_request: dict[str, Any]) -> bool:
        repo_owner = repo_full_name.split("/", 1)[0].lower()
        author_association = str(pull_request.get("author_association") or "").upper()
        if author_association == "OWNER":
            return True
        user = pull_request.get("user") or {}
        login = str(user.get("login") or "").lower() if isinstance(user, dict) else ""
        return bool(login) and login == repo_owner

    def _handle_final_verdict_agent_event(self, event: NormalizedEvent, signature: dict[str, str]) -> dict[str, Any]:
        pr_number = int(signature["pr"])
        event_key = self._agent_event_key(signature, default_pr=pr_number)
        if self.storage.has_processed_event(event_key):
            return {"status": "ignored", "reason": "duplicate_agent_event"}
        if not self._is_pr_open(event.repo_full_name, pr_number):
            return {"status": "ignored", "reason": "pr_not_open"}
        pull_request = self.github.get_pull_request(event.repo_full_name, pr_number)
        if not self._pull_request_author_is_repo_owner(event.repo_full_name, pull_request):
            self.storage.record_processed_event(event_key)
            return {"status": "approved", "reason": "human_merge_required", "pr_number": pr_number}
        try:
            self.github.merge_pull_request(event.repo_full_name, pr_number)
        except MergeConflictError as exc:
            repo = self.storage.get_repo(event.repo_full_name)
            if repo is None:
                return {"status": "ignored", "reason": "missing_repo"}
            issue_number = self._issue_number_for_signature_event(event.repo_full_name, signature, pr_number=pr_number)
            if issue_number is None:
                issue_number = self._extract_issue_number(pull_request.get("body"))
            merge_conflict_job = self._enqueue_job(
                repo,
                stage="merge_conflict_resolution",
                issue_number=issue_number,
                pr_number=pr_number,
                metadata={
                    "title": pull_request.get("title") or event.title or f"PR #{pr_number}",
                    "body": pull_request.get("body") or "",
                    "head_branch": self._branch_ref(pull_request, "head"),
                    "base_branch": self._branch_ref(pull_request, "base"),
                    "conflict_reason": str(exc),
                },
            )
            self.storage.record_processed_event(event_key)
            return {"status": "queued", "job_id": merge_conflict_job.id, "stage": merge_conflict_job.stage}
        self.storage.record_processed_event(event_key)
        return {"status": "merged", "pr_number": pr_number}

    def _handle_review_round_event(self, event: NormalizedEvent, signature: dict[str, str]) -> dict[str, Any]:
        event_key = self._agent_event_key(signature, default_pr=event.number if event.is_pull_request else None)
        if not self.storage.record_processed_event(event_key):
            return {"status": "ignored", "reason": "duplicate_agent_event"}
        review_round = int(signature["round"])
        pr_number = int(signature["pr"])
        issue_number = self._issue_number_for_signature_event(event.repo_full_name, signature, pr_number=pr_number)
        if issue_number is None:
            return {"status": "ignored", "reason": "untracked_pr"}
        source_job = self.storage.get_job(signature.get("job", ""))
        repo = self.storage.get_repo(event.repo_full_name)
        if repo is None:
            return {"status": "ignored", "reason": "missing_repo"}
        if not self._is_pr_open(event.repo_full_name, pr_number):
            return {"status": "ignored", "reason": "pr_not_open"}
        pr_metadata = self._pull_request_metadata(event.repo_full_name, pr_number)
        lineage_metadata = self._automation_lineage_metadata(source_job)
        if issue_number is None:
            return {"status": "ignored", "reason": "untracked_pr"}
        if not self._is_pr_open(event.repo_full_name, pr_number):
            return {"status": "ignored", "reason": "pr_not_open"}
        next_job = self._enqueue_job(
            repo,
            stage="implementation",
            issue_number=issue_number,
            pr_number=pr_number,
            review_round=review_round,
            metadata={
                **pr_metadata,
                **lineage_metadata,
                "title": (pr_metadata.get("title") or event.title or ""),
                "review_comment_body": event.body or "",
                "triggering_review_round": review_round,
            },
        )
        return {"status": "queued", "job_id": next_job.id, "stage": next_job.stage}

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
        stored_job = self.storage.get_job(job.id)
        if stored_job is not None and stored_job.status == "superseded":
            job.status = "superseded"
            return

        repo = self.storage.get_repo(job.repo_full_name)
        if repo is None:
            self.storage.update_job(job.id, status="failed", metadata={**job.metadata, "error": "missing repo"})
            job.status = "failed"
            return

        if job.stage == "dev_sync":
            self._run_dev_sync_job(repo, job)
            return

        retry_history: list[dict[str, str]] = list(job.metadata.get("retry_history", []))
        max_attempts = len(RETRY_BACKOFF_SECONDS) + 1

        for attempt in range(1, max_attempts + 1):
            try:
                self._run_job_attempt(repo, job)
            except TransientCapacityError as exc:
                if self._handle_transient_failure(repo, job, exc, attempt, max_attempts, retry_history):
                    return
            except Exception as exc:
                if self._handle_job_failure(job, exc, attempt, retry_history):
                    return
                job.status = "failed"
                return
            else:
                self._handle_job_success(job, attempt, retry_history)
                return

    def _handle_job_success(self, job: JobRecord, attempt: int, retry_history: list[dict[str, str]]) -> None:
        if job.stage in ISSUE_COMMENT_RECOVERY_STAGES:
            self._complete_comment_recovery(job)
        self.storage.update_job(
            job.id,
            status="completed",
            metadata={**job.metadata, "retry_attempts": attempt - 1, "retry_history": retry_history},
        )
        job.status = "completed"

    def _run_job_attempt(self, repo: RepoConfig, job: JobRecord) -> None:
        if job.stage in ISSUE_COMMENT_RECOVERY_STAGES:
            self._run_comment_recovery_attempt(repo, job)
            return

        preferred_runtime = self._preferred_runtime_for(job)
        lineage_session = self._lineage_session_for(job)
        initial_runtime = self._initial_runtime_for(job, preferred_runtime, lineage_session)
        try:
            session = self._execute_job_session(
                repo,
                job,
                runtime=initial_runtime,
                preferred_runtime=preferred_runtime,
                resume_session=lineage_session if job.stage == "issue_followup" else None,
            )
        except ClaudeUsageLimitError as exc:
            if initial_runtime != RUNTIME_OMO:
                raise
            bridge = self._bridge_context_for(repo, job, lineage_session)
            session = self._execute_job_session(
                repo,
                job,
                runtime=RUNTIME_OMX,
                preferred_runtime=preferred_runtime,
                bridge_context=bridge,
                fallback_reason=f"claude_{exc.limit_type}_limit",
                usage_limit_error=exc,
            )
        self.storage.update_job(
            job.id,
            status="launched",
            session_id=session.id,
            metadata={**job.metadata, "effective_runtime": session.effective_runtime or initial_runtime},
        )

    def _run_comment_recovery_attempt(self, repo: RepoConfig, job: JobRecord) -> None:
        preferred_runtime = self._preferred_runtime_for(job)
        runtime = self._runtime_for_comment_recovery_resume(job, preferred_runtime=preferred_runtime)
        prompt = self._build_prompt(repo, job, runtime=runtime)
        runner = self._runner_for_runtime(runtime)
        resume_session_id = self._recovery_resume_session_id(job, runtime=runtime)
        if resume_session_id:
            try:
                session = runner.resume(Path(repo.local_path), job, prompt, resume_session_id)
            except Exception as exc:
                job.metadata["comment_recovery_resume_error"] = str(exc)
                if self._comment_recovery_side_effect_exists(repo, job):
                    self._mark_comment_recovery_side_effect_already_posted(job)
                    return
                session = runner.launch(Path(repo.local_path), job, prompt)
            else:
                try:
                    self._wait_for_comment_recovery_session(
                        runner, repo, job, session, preferred_runtime=preferred_runtime, runtime=runtime
                    )
                except Exception as exc:
                    job.metadata["comment_recovery_resume_error"] = str(exc)
                    if self._comment_recovery_side_effect_exists(repo, job):
                        self._mark_comment_recovery_side_effect_already_posted(job)
                        return
                    session = runner.launch(Path(repo.local_path), job, prompt)
                else:
                    return
        else:
            session = runner.launch(Path(repo.local_path), job, prompt)
        self._wait_for_comment_recovery_session(
            runner, repo, job, session, preferred_runtime=preferred_runtime, runtime=runtime
        )

    def _wait_for_comment_recovery_session(
        self,
        runner: AgentRunner,
        repo: RepoConfig,
        job: JobRecord,
        session: SessionRecord,
        *,
        preferred_runtime: str,
        runtime: str,
    ) -> None:
        self._annotate_session_record(
            session,
            preferred_runtime=preferred_runtime,
            effective_runtime=runtime,
            fallback_reason=None,
        )
        self.storage.create_session(session)
        self.storage.update_job(
            job.id,
            status="launched",
            session_id=session.id,
            metadata={
                **job.metadata,
                "effective_runtime": session.effective_runtime or runtime,
                "recovery_runtime_handle": session.runtime_handle,
            },
        )
        job.session_id = session.id
        job.metadata["effective_runtime"] = session.effective_runtime or runtime
        job.metadata["recovery_runtime_handle"] = session.runtime_handle
        try:
            runner.wait(session.runtime_handle)
            self._verify_side_effect(repo, job)
        except Exception:
            self._finalize_session(session, status="failed", termination_reason="failed")
            raise
        self._finalize_session(session, status="completed", termination_reason="completed")

    def _recovery_resume_session_id(self, job: JobRecord, *, runtime: str) -> str | None:
        source_session_id = job.metadata.get("source_omx_session_id")
        if not isinstance(source_session_id, str) or not source_session_id:
            return None
        runner = self._runner_for_runtime(runtime)
        if not runner.can_resume(source_session_id):
            return None
        return source_session_id

    def _runtime_for_comment_recovery_resume(self, job: JobRecord, *, preferred_runtime: str) -> str:
        source_runtime = job.metadata.get("source_effective_runtime") or job.metadata.get(
            "source_native_session_runtime"
        )
        source_session_id = job.metadata.get("source_omx_session_id")
        if not isinstance(source_runtime, str) or not source_runtime:
            return preferred_runtime
        if not isinstance(source_session_id, str) or not source_session_id:
            return preferred_runtime
        runtime = normalize_runtime(source_runtime)
        if self._runner_for_runtime(runtime).can_resume(source_session_id):
            return runtime
        return preferred_runtime

    def _comment_recovery_side_effect_exists(self, repo: RepoConfig, job: JobRecord) -> bool:
        with contextlib.suppress(Exception):
            self._verify_side_effect(repo, job)
            return True
        return False

    def _mark_comment_recovery_side_effect_already_posted(self, job: JobRecord) -> None:
        metadata = {**job.metadata, "note": "side_effect_already_posted"}
        self.storage.update_job(job.id, metadata=metadata)
        job.metadata = metadata

    def _execute_job_session(
        self,
        repo: RepoConfig,
        job: JobRecord,
        *,
        runtime: str,
        preferred_runtime: str,
        resume_session: SessionRecord | None = None,
        bridge_context: BridgeContext | None = None,
        fallback_reason: str | None = None,
        usage_limit_error: ClaudeUsageLimitError | None = None,
    ) -> SessionRecord:
        prompt = self._build_prompt(
            repo,
            job,
            runtime=runtime,
            bridge_prompt=(bridge_context.prompt_block if bridge_context else ""),
        )
        runner = self._runner_for_runtime(runtime)
        if resume_session is not None and self._resume_runtime_for_session(resume_session) == runtime:
            session = runner.resume(
                Path(repo.local_path), job, prompt, self._session_id_for_resume(job, resume_session)
            )
        else:
            session = runner.launch(Path(repo.local_path), job, prompt)

        self._annotate_session_record(
            session,
            preferred_runtime=preferred_runtime,
            effective_runtime=runtime,
            fallback_reason=fallback_reason,
            bridge_context=bridge_context,
        )
        self.storage.create_session(session)
        self.storage.update_job(
            job.id,
            status="launched",
            session_id=session.id,
            metadata={**job.metadata, "effective_runtime": session.effective_runtime or runtime},
        )
        job.status = "launched"
        job.session_id = session.id
        job.metadata["effective_runtime"] = session.effective_runtime or runtime
        try:
            runner.wait(session.runtime_handle, timeout_seconds=self.config.agent_timeout_seconds)
            self._verify_side_effect(repo, job)
        except Exception:
            self._finalize_session(session, status="failed", termination_reason="failed")
            raise
        if session.omx_session_id is None:
            post_wait_session_id = runner.get_session_id(session.runtime_handle)
            if post_wait_session_id:
                session.omx_session_id = post_wait_session_id
                self.storage.update_session(
                    session.id,
                    omx_session_id=post_wait_session_id,
                    native_session_runtime=session.native_session_runtime or runtime,
                    effective_runtime=session.effective_runtime or runtime,
                )
        self._finalize_session(session, status="completed", termination_reason="completed")
        self._update_job_runtime_metadata(
            job,
            preferred_runtime=preferred_runtime,
            effective_runtime=runtime,
            fallback_reason=fallback_reason,
            bridge_context=bridge_context,
            usage_limit_error=usage_limit_error,
            session=session,
        )
        return session

    def _annotate_session_record(
        self,
        session: SessionRecord,
        *,
        preferred_runtime: str,
        effective_runtime: str,
        fallback_reason: str | None,
        bridge_context: BridgeContext | None = None,
    ) -> None:
        session.preferred_runtime = preferred_runtime
        session.effective_runtime = effective_runtime
        session.native_session_runtime = effective_runtime
        session.fallback_reason = fallback_reason
        if bridge_context is not None:
            session.bridge_source_runtime = bridge_context.source_runtime
            session.bridge_source_session_id = bridge_context.source_session_id

    def _update_job_runtime_metadata(
        self,
        job: JobRecord,
        *,
        preferred_runtime: str,
        effective_runtime: str,
        fallback_reason: str | None,
        bridge_context: BridgeContext | None = None,
        usage_limit_error: ClaudeUsageLimitError | None = None,
        session: SessionRecord,
    ) -> None:
        job.metadata["preferred_runtime"] = preferred_runtime
        job.metadata["effective_runtime"] = effective_runtime
        job.metadata["native_session_runtime"] = effective_runtime
        if fallback_reason:
            job.metadata["fallback_reason"] = fallback_reason
        if bridge_context is not None:
            if bridge_context.source_runtime:
                job.metadata["bridge_source_runtime"] = bridge_context.source_runtime
            if bridge_context.source_session_id:
                job.metadata["bridge_source_session_id"] = bridge_context.source_session_id
            if bridge_context.note:
                job.metadata["bridge_note"] = bridge_context.note
        if usage_limit_error is not None:
            job.metadata["usage_limit_runtime"] = RUNTIME_OMO
            job.metadata["usage_limit_kind"] = usage_limit_error.limit_type
            if usage_limit_error.reset_hint:
                job.metadata["usage_limit_reset_hint"] = usage_limit_error.reset_hint
            if usage_limit_error.suggested_retry_at:
                job.metadata["usage_limit_until"] = usage_limit_error.suggested_retry_at
        if session.omx_session_id:
            job.metadata["omx_session_id"] = session.omx_session_id

    def _runner_for_runtime(self, runtime: str) -> AgentRunner:
        normalized = normalize_runtime(runtime)
        runner = self._runtime_runners.get(normalized)
        if runner is None:
            runner = build_agent_runner(normalized, self.config.run_dir)
            self._runtime_runners[normalized] = runner
        return runner

    def _preferred_runtime_for(self, job: JobRecord) -> str:
        runtime = job.metadata.get("preferred_runtime")
        if isinstance(runtime, str) and runtime:
            return normalize_runtime(runtime)
        return normalize_runtime(self.config.agent_runtime)

    def _lineage_session_for(self, job: JobRecord) -> SessionRecord | None:
        if job.stage != "issue_followup":
            return None
        if job.issue_number is None:
            return None
        return self._latest_issue_lineage_session(job.repo_full_name, job.issue_number)

    def _initial_runtime_for(
        self,
        job: JobRecord,
        preferred_runtime: str,
        lineage_session: SessionRecord | None,
    ) -> str:
        if job.stage == "issue_followup" and lineage_session is not None:
            return self._resume_runtime_for_session(lineage_session)
        if preferred_runtime != RUNTIME_OMO:
            return preferred_runtime
        if self._has_active_omo_usage_limit(job.repo_full_name):
            job.metadata["fallback_reason"] = "cached_claude_usage_limit"
            return RUNTIME_OMX
        return preferred_runtime

    def _has_active_omo_usage_limit(self, repo_full_name: str) -> bool:
        now = datetime.now(tz=timezone.utc)
        for job in reversed(self.storage.list_jobs()):
            if job.repo_full_name != repo_full_name:
                continue
            if job.metadata.get("usage_limit_runtime") != RUNTIME_OMO:
                continue
            retry_at = job.metadata.get("usage_limit_until")
            if not isinstance(retry_at, str) or not retry_at:
                continue
            try:
                retry_at_dt = datetime.fromisoformat(retry_at)
            except ValueError:
                continue
            if retry_at_dt.tzinfo is None:
                retry_at_dt = retry_at_dt.replace(tzinfo=timezone.utc)
            if retry_at_dt > now:
                return True
        return False

    def _session_id_for_resume(self, job: JobRecord, session: SessionRecord) -> str:
        if session.omx_session_id:
            return session.omx_session_id
        return self._omx_session_id_for(job)

    def _resume_runtime_for_session(self, session: SessionRecord) -> str:
        explicit_runtime = session.effective_runtime or session.native_session_runtime or session.preferred_runtime
        if explicit_runtime:
            return normalize_runtime(explicit_runtime)
        if session.omx_session_id and self.omx_runner.can_resume(session.omx_session_id):
            return normalize_runtime(self.config.agent_runtime)
        inferred_runtime = effective_session_runtime(session)
        if inferred_runtime:
            return inferred_runtime
        return normalize_runtime(self.config.agent_runtime)

    def _bridge_context_for(
        self, repo: RepoConfig, job: JobRecord, lineage_session: SessionRecord | None
    ) -> BridgeContext | None:
        source_session_id = None
        if lineage_session is not None and effective_session_runtime(lineage_session) == RUNTIME_OMO:
            source_session_id = lineage_session.omx_session_id
        bridge = self.session_bridge.load(repo_path=Path(repo.local_path), session_id=source_session_id)
        if bridge is None:
            return None
        if not bridge.prompt_block and not bridge.note:
            return None
        return bridge

    def _handle_transient_failure(
        self,
        repo: RepoConfig,
        job: JobRecord,
        exc: TransientCapacityError,
        attempt: int,
        max_attempts: int,
        retry_history: list[dict[str, str]],
    ) -> bool:
        """Handle a transient capacity error. Return True if the job is terminal (done or exhausted)."""
        # If the side effect was already posted before the capacity error,
        # treat the job as completed to avoid duplicate GitHub comments/PRs.
        side_effect_exists = False
        with contextlib.suppress(Exception):
            self._verify_side_effect(repo, job)
            side_effect_exists = True
        if side_effect_exists:
            if job.stage in ISSUE_COMMENT_RECOVERY_STAGES:
                self._complete_comment_recovery(job)
            self.storage.update_job(
                job.id,
                status="completed",
                metadata={
                    **job.metadata,
                    "retry_attempts": attempt - 1,
                    "retry_history": retry_history,
                    "note": "side_effect_already_posted",
                },
            )
            job.status = "completed"
            return True
        retry_history.append({
            "attempt": str(attempt),
            "reason": "transient_capacity",
            "pattern": exc.pattern,
            "at": utc_now(),
        })
        if attempt >= max_attempts:
            logger.warning("Job %s exhausted all %d retry attempts", job.id, max_attempts)
            self.storage.update_job(
                job.id,
                status="failed",
                metadata={
                    **job.metadata,
                    "error": f"retry_exhausted: {exc}",
                    "retry_exhausted": True,
                    "retry_attempts": attempt,
                    "retry_history": retry_history,
                },
            )
            if job.stage in ISSUE_COMMENT_RECOVERY_STAGES:
                self._fail_comment_recovery(job, RuntimeError(f"retry_exhausted: {exc}"), attempt, retry_history)
            job.status = "failed"
            return True
        backoff = RETRY_BACKOFF_SECONDS[attempt - 1]
        logger.info("Job %s attempt %d hit transient capacity error, retrying in %ds", job.id, attempt, backoff)
        self.storage.update_job(
            job.id,
            status="retrying",
            metadata={**job.metadata, "retry_attempts": attempt, "retry_history": retry_history},
        )
        time.sleep(backoff)
        return False

    def _handle_job_failure(
        self,
        job: JobRecord,
        exc: Exception,
        attempt: int,
        retry_history: list[dict[str, str]],
    ) -> bool:
        if job.stage in ISSUE_COMMENT_RECOVERY_STAGES:
            self._fail_comment_recovery(job, exc, attempt, retry_history)
            return False
        if self._should_recover_missing_issue_comment(job, exc) and self._enqueue_comment_recovery(job, exc):
            return True
        metadata = {
            **job.metadata,
            "error": str(exc),
            "retry_attempts": attempt - 1,
            "retry_history": retry_history,
        }
        if isinstance(exc, ClaudeUsageLimitError):
            metadata["error"] = "claude_usage_limit"
            metadata["usage_limit_runtime"] = RUNTIME_OMO
            metadata["usage_limit_kind"] = exc.limit_type
            if exc.reset_hint:
                metadata["usage_limit_reset_hint"] = exc.reset_hint
            if exc.suggested_retry_at:
                metadata["usage_limit_until"] = exc.suggested_retry_at
        if self._is_rollout_missing_error(exc):
            metadata["error"] = "rollout_missing"
            metadata["error_detail"] = str(exc)
            with contextlib.suppress(Exception):
                self._post_session_lost_warning(job)
        self.storage.update_job(job.id, status="failed", metadata=metadata)
        return False

    def _should_recover_missing_issue_comment(self, job: JobRecord, exc: Exception) -> bool:
        return job.stage in {"issue_request", "issue_followup"} and str(exc) in ISSUE_COMMENT_MISSING_ERRORS

    def _enqueue_comment_recovery(self, job: JobRecord, exc: Exception) -> bool:
        original_error = str(exc)
        attempts = int(job.metadata.get("comment_recovery_attempts") or 0)
        existing = self._existing_comment_recovery_job(job)
        if existing is not None and existing.status in {"queued", "launched", "retrying"}:
            self.storage.update_job(
                job.id,
                status="recovering",
                metadata={
                    **job.metadata,
                    "error": original_error,
                    "original_error": original_error,
                    "comment_recovery_attempts": attempts,
                    "comment_recovery_job_id": existing.id,
                },
            )
            job.status = "recovering"
            return True
        if attempts >= MAX_COMMENT_RECOVERY_ATTEMPTS:
            return False

        repo = self.storage.get_repo(job.repo_full_name)
        if repo is None or job.issue_number is None:
            return False
        recovery_stage = ISSUE_REQUEST_RECOVERY_STAGE if job.stage == "issue_request" else ISSUE_FOLLOWUP_RECOVERY_STAGE
        expected_signature = build_signature(stage=job.stage, job=job.id, issue=int(job.issue_number))
        source_session = self.storage.find_latest_session(
            repo_full_name=job.repo_full_name,
            stage=job.stage,
            job_id=job.id,
            issue_number=job.issue_number,
        )
        recovery_attempt = attempts + 1
        recovery_job = self._enqueue_job(
            repo,
            stage=recovery_stage,
            issue_number=job.issue_number,
            metadata={
                "title": job.metadata.get("title", ""),
                "body": job.metadata.get("body", ""),
                "comment_body": job.metadata.get("comment_body", ""),
                "source_job_id": job.id,
                "source_stage": job.stage,
                "original_error": original_error,
                "expected_signature": expected_signature,
                "comment_recovery_attempt": recovery_attempt,
                "preferred_runtime": job.metadata.get(
                    "preferred_runtime", normalize_runtime(self.config.agent_runtime)
                ),
                "source_omx_session_id": (
                    source_session.omx_session_id
                    if source_session is not None and source_session.omx_session_id
                    else job.metadata.get("omx_session_id")
                ),
                "source_preferred_runtime": (source_session.preferred_runtime if source_session is not None else None),
                "source_effective_runtime": (
                    effective_session_runtime(source_session) if source_session is not None else None
                ),
                "source_native_session_runtime": (
                    source_session.native_session_runtime if source_session is not None else None
                ),
            },
        )
        self.storage.update_job(
            job.id,
            status="recovering",
            metadata={
                **job.metadata,
                "error": original_error,
                "original_error": original_error,
                "comment_recovery_attempts": recovery_attempt,
                "comment_recovery_job_id": recovery_job.id,
            },
        )
        job.status = "recovering"
        job.metadata = {
            **job.metadata,
            "error": original_error,
            "original_error": original_error,
            "comment_recovery_attempts": recovery_attempt,
            "comment_recovery_job_id": recovery_job.id,
        }
        return True

    def _existing_comment_recovery_job(self, job: JobRecord) -> JobRecord | None:
        recovery_stages = set(ISSUE_COMMENT_RECOVERY_STAGES)
        for candidate in reversed(
            self.storage.find_jobs(repo_full_name=job.repo_full_name, issue_number=job.issue_number)
        ):
            if candidate.stage not in recovery_stages:
                continue
            if candidate.metadata.get("source_job_id") == job.id:
                return candidate
        return None

    def _complete_comment_recovery(self, job: JobRecord) -> None:
        source_job_id = str(job.metadata.get("source_job_id") or "")
        if not source_job_id:
            return
        source = self.storage.get_job(source_job_id)
        if source is None:
            return
        metadata = {
            **source.metadata,
            "error": None,
            "original_error": job.metadata.get("original_error"),
            "comment_recovery_attempts": job.metadata.get("comment_recovery_attempt", 1),
            "comment_recovery_job_id": job.id,
            "comment_recovery_session_id": job.session_id,
            "comment_recovery_effective_runtime": job.metadata.get("effective_runtime"),
            "comment_recovery_runtime_handle": job.metadata.get("recovery_runtime_handle"),
            "comment_recovery_resume_error": job.metadata.get("comment_recovery_resume_error"),
        }
        self.storage.update_job(source.id, status="completed", metadata=metadata)

    def _fail_comment_recovery(
        self,
        job: JobRecord,
        exc: Exception,
        attempt: int,
        retry_history: list[dict[str, str]],
    ) -> None:
        source_job_id = str(job.metadata.get("source_job_id") or "")
        original_error = str(job.metadata.get("original_error") or str(exc))
        metadata = {
            **job.metadata,
            "error": str(exc),
            "retry_attempts": attempt - 1,
            "retry_history": retry_history,
        }
        self.storage.update_job(job.id, status="failed", metadata=metadata)
        source = self.storage.get_job(source_job_id) if source_job_id else None
        if source is None:
            return
        self.storage.update_job(
            source.id,
            status="failed",
            metadata={
                **source.metadata,
                "error": original_error,
                "original_error": original_error,
                "comment_recovery_attempts": job.metadata.get("comment_recovery_attempt", 1),
                "comment_recovery_job_id": job.id,
                "comment_recovery_session_id": job.session_id,
                "comment_recovery_effective_runtime": job.metadata.get("effective_runtime"),
                "comment_recovery_runtime_handle": job.metadata.get("recovery_runtime_handle"),
                "comment_recovery_resume_error": job.metadata.get("comment_recovery_resume_error"),
                "comment_recovery_last_error": str(exc),
            },
        )

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
            preferred_runtime = self._preferred_runtime_for(job)
            runtime = preferred_runtime
            fallback_reason = None
            if preferred_runtime == RUNTIME_OMO and self._has_active_omo_usage_limit(job.repo_full_name):
                runtime = RUNTIME_OMX
                fallback_reason = "cached_claude_usage_limit"
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
                runtime=runtime,
            )
            runner = self._runner_for_runtime(runtime)
            try:
                session = runner.launch(conflict_context.worktree_path, job, prompt)
                self._annotate_session_record(
                    session,
                    preferred_runtime=preferred_runtime,
                    effective_runtime=runtime,
                    fallback_reason=fallback_reason,
                )
                self.storage.create_session(session)
                self.storage.update_job(
                    job.id,
                    status="launched",
                    session_id=session.id,
                    metadata={
                        **job.metadata,
                        "preferred_runtime": preferred_runtime,
                        "effective_runtime": runtime,
                        "native_session_runtime": runtime,
                        "fallback_reason": fallback_reason,
                        "sync_status": "conflict",
                        "worktree_path": str(conflict_context.worktree_path),
                    },
                )
                job.metadata = {
                    **job.metadata,
                    "preferred_runtime": preferred_runtime,
                    "effective_runtime": runtime,
                    "native_session_runtime": runtime,
                    "fallback_reason": fallback_reason,
                    "sync_status": "conflict",
                    "worktree_path": str(conflict_context.worktree_path),
                }
                runner.wait(session.runtime_handle, timeout_seconds=self.config.agent_timeout_seconds)
            except ClaudeUsageLimitError as limit_exc:
                if runtime != RUNTIME_OMO:
                    raise
                if session is not None:
                    self._finalize_session(session, status="failed", termination_reason="failed")
                runtime = RUNTIME_OMX
                fallback_reason = f"claude_{limit_exc.limit_type}_limit"
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
                    runtime=runtime,
                )
                runner = self._runner_for_runtime(runtime)
                session = runner.launch(conflict_context.worktree_path, job, prompt)
                self._annotate_session_record(
                    session,
                    preferred_runtime=preferred_runtime,
                    effective_runtime=runtime,
                    fallback_reason=fallback_reason,
                )
                self.storage.create_session(session)
                self.storage.update_job(
                    job.id,
                    status="launched",
                    session_id=session.id,
                    metadata={
                        **job.metadata,
                        "preferred_runtime": preferred_runtime,
                        "effective_runtime": runtime,
                        "native_session_runtime": runtime,
                        "fallback_reason": fallback_reason,
                        "usage_limit_runtime": RUNTIME_OMO,
                        "usage_limit_kind": limit_exc.limit_type,
                        "usage_limit_until": limit_exc.suggested_retry_at,
                        "usage_limit_reset_hint": limit_exc.reset_hint,
                        "sync_status": "conflict",
                        "worktree_path": str(conflict_context.worktree_path),
                    },
                )
                job.metadata = {
                    **job.metadata,
                    "preferred_runtime": preferred_runtime,
                    "effective_runtime": runtime,
                    "native_session_runtime": runtime,
                    "fallback_reason": fallback_reason,
                    "usage_limit_runtime": RUNTIME_OMO,
                    "usage_limit_kind": limit_exc.limit_type,
                    "usage_limit_until": limit_exc.suggested_retry_at,
                    "usage_limit_reset_hint": limit_exc.reset_hint,
                    "sync_status": "conflict",
                    "worktree_path": str(conflict_context.worktree_path),
                }
                runner.wait(session.runtime_handle, timeout_seconds=self.config.agent_timeout_seconds)
            self.dev_syncer.verify_remote_sync(conflict_context)
            self._finalize_session(session, status="completed", termination_reason="completed")
            self.storage.update_job(
                job.id,
                status="completed",
                metadata={**job.metadata, "effective_runtime": runtime, "sync_status": "resolved_with_agent"},
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
        runtime = (
            effective_session_runtime(session)
            or session.native_session_runtime
            or normalize_runtime(self.config.agent_runtime)
        )
        runner = self._runner_for_runtime(runtime)
        try:
            runner.close_session(session.runtime_handle)
        finally:
            self.storage.update_session(
                session.id,
                status=status,
                ended_at=utc_now(),
                termination_reason=termination_reason,
            )

    def _build_prompt(
        self,
        repo: RepoConfig,
        job: JobRecord,
        *,
        runtime: str | None = None,
        bridge_prompt: str = "",
    ) -> str:
        resolved_runtime = normalize_runtime(runtime or self.config.agent_runtime)
        issue_number = job.issue_number or 0
        pr_number = job.pr_number or 0
        issue_context = self._issue_metadata(repo.full_name, issue_number) if issue_number else {}
        issue_title = issue_context.get("title", job.metadata.get("title", f"Issue #{issue_number}"))
        issue_body = issue_context.get("body", job.metadata.get("body", ""))
        pr_snapshot = self._pull_request_metadata(repo.full_name, pr_number) if pr_number else {}
        pr_title = pr_snapshot.get("title", job.metadata.get("title", f"PR #{pr_number}"))
        pr_body = pr_snapshot.get("body", job.metadata.get("body", ""))
        if job.stage == "issue_request":
            prompt = self._build_issue_request_prompt(
                repo, job, issue_number, issue_title, issue_body, resolved_runtime
            )
            return self._apply_bridge_context(prompt, bridge_prompt)

        if job.stage == "implementation":
            prompt = self._build_implementation_prompt(
                repo,
                job,
                issue_number,
                issue_title,
                issue_body,
                pr_number,
                pr_title,
                pr_body,
                resolved_runtime,
            )
            return self._apply_bridge_context(prompt, bridge_prompt)

        if job.stage == "issue_followup":
            prompt = self._build_issue_followup_prompt(
                repo,
                job,
                issue_number,
                issue_title,
                issue_body,
                resolved_runtime,
            )
            return self._apply_bridge_context(prompt, bridge_prompt)

        if job.stage in ISSUE_COMMENT_RECOVERY_STAGES:
            return self._build_issue_comment_recovery_prompt(repo, job, issue_number, issue_title, issue_body)

        if job.stage == "review_round":
            prompt = self._build_review_round_prompt(
                repo,
                job,
                issue_number,
                pr_number,
                pr_title,
                pr_body,
                resolved_runtime,
            )
            return self._apply_bridge_context(prompt, bridge_prompt)

        if job.stage == "merge_conflict_resolution":
            prompt = self._build_merge_conflict_resolution_prompt(
                repo,
                job,
                issue_number,
                pr_number,
                pr_title,
                pr_body,
                resolved_runtime,
            )
            return self._apply_bridge_context(prompt, bridge_prompt)

        prompt = self._build_final_verdict_prompt(job, issue_number, pr_number, pr_title, pr_body, resolved_runtime)
        return self._apply_bridge_context(prompt, bridge_prompt)

    def _apply_bridge_context(self, prompt: str, bridge_prompt: str) -> str:
        guarded_prompt = ensure_non_interactive_guard(prompt)
        if not bridge_prompt.strip():
            return guarded_prompt
        bridge_block = (
            f"{bridge_prompt}\n\n"
            "Use the imported OMO context above only as bounded background context. "
            "This is not a native resume; continue from it conservatively."
        )
        prompt_body = split_non_interactive_guard(guarded_prompt)
        return f"{NON_INTERACTIVE_GUARD}\n{bridge_block}\n\n{prompt_body}"

    def _verify_side_effect(self, repo: RepoConfig, job: JobRecord) -> None:
        if job.stage == "issue_request":
            self._verify_issue_request_side_effect(repo, job)
            return
        if job.stage == "issue_followup":
            self._verify_issue_followup_side_effect(repo, job)
            return
        if job.stage in ISSUE_COMMENT_RECOVERY_STAGES:
            self._verify_issue_comment_recovery_side_effect(repo, job)
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
            return
        raise RuntimeError("side-effect-verification-unsupported")

    def _build_issue_request_prompt(
        self,
        repo: RepoConfig,
        job: JobRecord,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        runtime: str,
    ) -> str:
        return render_prompt(
            "issue_request",
            {
                "repo": repo.full_name,
                "local_path": repo.local_path,
                "issue_number": issue_number,
                "issue_title": issue_title,
                "issue_body": issue_body,
                "discussion": self._render_issue_discussion(repo.full_name, issue_number),
                "signature": build_signature(stage="issue_request", job=job.id, issue=issue_number),
            },
            runtime=runtime,
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
        runtime: str,
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
            runtime=runtime,
        )

    def _build_issue_followup_prompt(
        self,
        repo: RepoConfig,
        job: JobRecord,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        runtime: str,
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
            runtime=runtime,
        )

    def _build_issue_comment_recovery_prompt(
        self,
        repo: RepoConfig,
        job: JobRecord,
        issue_number: int,
        issue_title: str,
        issue_body: str,
    ) -> str:
        original_stage = ISSUE_COMMENT_RECOVERY_STAGES[job.stage]
        source_job_id = str(job.metadata.get("source_job_id", ""))
        expected_signature = str(job.metadata.get("expected_signature", ""))
        original_error = str(job.metadata.get("original_error", ""))
        comment_body = str(job.metadata.get("comment_body", ""))
        prompt = (
            f"You are operating inside repository: {repo.full_name}\n"
            f"Local path: {repo.local_path}\n"
            f"Recovery task for GitHub issue #{issue_number}: {issue_title}\n\n"
            "A previous Dani OMX session ended without leaving the required GitHub issue comment, "
            f"so side-effect verification failed with `{original_error}`.\n\n"
            "Strict recovery instructions:\n"
            "- Do not write code, edit files, create branches, open PRs, or run implementation work.\n"
            "- Leave one GitHub issue comment exactly once, then exit.\n"
            "- The comment must include the exact Dani signature below unchanged; do not replace it with this "
            "recovery job's id.\n"
            "- Keep the comment concise and self-contained for the visible issue discussion.\n\n"
            f"Repository: {repo.full_name}\n"
            f"Issue number: {issue_number}\n"
            f"Issue title: {issue_title}\n\n"
            f"Original issue body:\n{issue_body}\n\n"
            f"Latest follow-up comment, if any:\n{comment_body}\n\n"
            f"Original job id: {source_job_id}\n"
            f"Original stage: {original_stage}\n"
            f"Required exact Dani signature:\n{expected_signature}\n\n"
            "Post it with gh (write the comment to a file first, then send it):\n"
            f"gh issue comment {issue_number} --repo {repo.full_name} --body-file <recovery-comment.md>\n\n"
            "After posting the comment, exit."
        )
        return ensure_non_interactive_guard(prompt)

    def _build_review_round_prompt(
        self,
        repo: RepoConfig,
        job: JobRecord,
        issue_number: int,
        pr_number: int,
        pr_title: str,
        pr_body: str,
        runtime: str,
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
                "round_total": self.config.review_rounds,
                "review_mode_note": "",
                "signature": build_signature(**signature_fields),
            },
            runtime=runtime,
        )

    def _build_merge_conflict_resolution_prompt(
        self,
        repo: RepoConfig,
        job: JobRecord,
        issue_number: int,
        pr_number: int,
        pr_title: str,
        pr_body: str,
        runtime: str,
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
            runtime=runtime,
        )

    def _build_final_verdict_prompt(
        self,
        job: JobRecord,
        issue_number: int,
        pr_number: int,
        pr_title: str,
        pr_body: str,
        runtime: str,
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
                "review_cycle": "",
                "approve_signature": build_signature(
                    stage="final_verdict", job=job.id, pr=pr_number, verdict="APPROVE"
                ),
                "reject_signature": build_signature(stage="final_verdict", job=job.id, pr=pr_number, verdict="REJECT"),
            },
            runtime=runtime,
        )

    def _verify_issue_request_side_effect(self, repo: RepoConfig, job: JobRecord) -> None:
        signature = build_signature(stage="issue_request", job=job.id, issue=int(job.issue_number or 0))
        if not self._has_exact_issue_signature(repo.full_name, int(job.issue_number or 0), signature):
            raise RuntimeError("issue-request-comment-missing")

    def _verify_issue_followup_side_effect(self, repo: RepoConfig, job: JobRecord) -> None:
        signature = build_signature(stage="issue_followup", job=job.id, issue=int(job.issue_number or 0))
        if not self._has_exact_issue_signature(repo.full_name, int(job.issue_number or 0), signature):
            raise RuntimeError("issue-followup-comment-missing")

    def _verify_issue_comment_recovery_side_effect(self, repo: RepoConfig, job: JobRecord) -> None:
        signature = str(job.metadata.get("expected_signature") or "")
        if not signature:
            raise RuntimeError("issue-comment-recovery-missing-signature")
        if not self._has_exact_issue_signature(repo.full_name, int(job.issue_number or 0), signature):
            original_error = str(job.metadata.get("original_error") or "issue-comment-missing")
            raise RuntimeError(original_error)

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

    def _post_retarget_request_if_missing(self, repo: RepoConfig, event: NormalizedEvent) -> None:
        pr_number = event.number
        signature = build_signature(stage=RETARGET_REQUEST_STAGE, pr=pr_number)
        if self._has_exact_pr_signature(repo.full_name, pr_number, signature):
            return
        body = (
            f"Thanks for the contribution! \U0001f64f\n\n"
            f"This repository's automation only reviews pull requests that target the "
            f"`{repo.dev_branch}` branch (this PR currently targets `{event.base_branch}`). "
            f"Please change the base branch to `{repo.dev_branch}` so dani can pick it up "
            f"for automated review.\n\n"
            f'GitHub UI \u2192 click "Edit" next to the PR title \u2192 change the base '
            f"branch to `{repo.dev_branch}`, then push (or reopen) the PR.\n\n"
            f"{signature}"
        )
        try:
            self.github.create_pr_comment(repo.full_name, pr_number, body)
        except Exception:
            logger.warning("failed to post retarget-to-dev comment for %s#%s", repo.full_name, pr_number, exc_info=True)

    def _has_exact_issue_signature(self, repo_full_name: str, issue_number: int, signature: str) -> bool:
        return bool(
            self.github.find_comments_by_signature(
                repo_full_name, issue_number, kind="issue", signature_fragment=signature
            )
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

    def _has_existing_implementation_job(self, repo_full_name: str, issue_number: int) -> bool:
        for job in self.storage.find_jobs(
            repo_full_name=repo_full_name, stage="implementation", issue_number=issue_number
        ):
            if job.status in {"queued", "launched", "running", "completed"}:
                return True
        return False

    def _completed_followup_count(self, repo_full_name: str, issue_number: int) -> int:
        count = 0
        for job in self.storage.find_jobs(
            repo_full_name=repo_full_name, stage="issue_followup", issue_number=issue_number
        ):
            if job.status in {"queued", "launched", "running", "completed"}:
                count += 1
        return count

    def _is_dani_self_authored(self, event: NormalizedEvent) -> bool:
        configured_login = self.config.bot_login
        if configured_login:
            return event.actor_login == configured_login
        return event.actor_type == "Bot"

    def _handle_pull_request_closed(self, repo: RepoConfig, event: NormalizedEvent) -> dict[str, Any]:
        merged = bool(event.pr_merged)
        self.storage.mark_terminal_pr(repo.full_name, event.number, merged=merged)
        if merged:
            issue_number = self._extract_issue_number(event.body)
            if issue_number is not None:
                self.storage.mark_terminal_issue(repo.full_name, issue_number)
        return {"status": "marked_terminal", "pr_number": event.number, "merged": merged}

    def _queue_implementation(self, repo: RepoConfig, event: NormalizedEvent) -> dict[str, Any]:
        job = self._enqueue_job(
            repo,
            stage="implementation",
            issue_number=event.number,
            metadata={"title": event.title or "", "body": event.payload.get("issue", {}).get("body", "")},
        )
        return {"status": "queued", "job_id": job.id, "stage": job.stage}

    def _queue_issue_followup(self, repo: RepoConfig, event: NormalizedEvent) -> dict[str, Any]:
        session = self._latest_issue_lineage_session(event.repo_full_name, event.number)
        if session is None or session.omx_session_id is None:
            return {"status": "ignored", "reason": "missing_issue_session"}
        lineage_runtime = self._resume_runtime_for_session(session)
        lineage_runner = self._runner_for_runtime(lineage_runtime)
        if not lineage_runner.can_resume(session.omx_session_id):
            rerouted_job = self._enqueue_job(
                repo,
                stage="issue_request",
                issue_number=event.number,
                metadata={
                    "title": event.title or "",
                    "body": event.payload.get("issue", {}).get("body", ""),
                    "comment_body": event.body or "",
                    "rerouted_from": "issue_followup",
                    "prior_session_id": session.omx_session_id,
                    "preferred_runtime": session.preferred_runtime or normalize_runtime(self.config.agent_runtime),
                    "effective_runtime": effective_session_runtime(session),
                    "native_session_runtime": session.native_session_runtime,
                    "fallback_reason": session.fallback_reason,
                    "bridge_source_runtime": session.bridge_source_runtime,
                    "bridge_source_session_id": session.bridge_source_session_id,
                },
            )
            return {"status": "queued", "job_id": rerouted_job.id, "stage": rerouted_job.stage}
        job = self._enqueue_job(
            repo,
            stage="issue_followup",
            issue_number=event.number,
            metadata={
                "title": event.title or "",
                "body": event.payload.get("issue", {}).get("body", ""),
                "comment_body": event.body or "",
                "omx_session_id": session.omx_session_id,
                "preferred_runtime": session.preferred_runtime or normalize_runtime(self.config.agent_runtime),
                "effective_runtime": effective_session_runtime(session),
                "native_session_runtime": session.native_session_runtime,
                "fallback_reason": session.fallback_reason,
                "bridge_source_runtime": session.bridge_source_runtime,
                "bridge_source_session_id": session.bridge_source_session_id,
            },
        )
        return {"status": "queued", "job_id": job.id, "stage": job.stage}

    def _latest_issue_lineage_session(self, repo_full_name: str, issue_number: int) -> SessionRecord | None:
        for session in reversed(self.storage.list_sessions()):
            if session.repo_full_name != repo_full_name:
                continue
            if session.issue_number != issue_number:
                continue
            if session.stage not in {"issue_request", "issue_followup"}:
                continue
            if not session.omx_session_id:
                continue
            return session
        return None

    def _omx_session_id_for(self, job: JobRecord) -> str:
        omx_session_id = job.metadata.get("omx_session_id")
        if isinstance(omx_session_id, str) and omx_session_id:
            return omx_session_id
        msg = "missing-omx-session-id"
        raise RuntimeError(msg)

    def _queue_pull_request_review(
        self, repo: RepoConfig, event: NormalizedEvent, signature: dict[str, str] | None
    ) -> dict[str, Any]:
        is_agent_managed_pr = bool(signature and signature.get("stage") == "implementation")
        guard_result = self._external_pull_request_guard(repo, event, is_agent_managed_pr=is_agent_managed_pr)
        if guard_result is not None:
            return guard_result

        issue_number = self._pull_request_issue_number(event, signature)
        if is_agent_managed_pr:
            if event.action != "opened":
                return {"status": "ignored", "reason": "agent_managed_pr_followup"}
            job = self._enqueue_job(
                repo,
                stage="review_round",
                issue_number=issue_number,
                pr_number=event.number,
                review_round=1,
                metadata={"title": event.title or "", "body": event.body or ""},
            )
            return {"status": "queued", "job_id": job.id, "stage": job.stage}

        return self._queue_external_pull_request_review(
            repo, event, issue_number=issue_number, untracked=issue_number is None
        )

    def _external_pull_request_guard(
        self, repo: RepoConfig, event: NormalizedEvent, *, is_agent_managed_pr: bool
    ) -> dict[str, Any] | None:
        is_release_pr = event.head_branch == repo.dev_branch and event.base_branch == repo.main_branch
        if is_release_pr:
            return {"status": "ignored", "reason": "release_loop_excluded"}
        if not is_agent_managed_pr and event.base_branch is not None and event.base_branch != repo.dev_branch:
            self._post_retarget_request_if_missing(repo, event)
            return {"status": "ignored", "reason": "non_dev_target_branch"}
        if event.base_branch == repo.main_branch:
            return {"status": "ignored", "reason": "release_loop_excluded"}
        if not is_agent_managed_pr and self._external_contributor_account_too_new(event):
            event_key = self._external_pull_request_event_key(event)
            if not self.storage.record_processed_event(event_key):
                return {"status": "ignored", "reason": "duplicate_external_pr_event"}
            return self._close_ineligible_external_pull_request(event)
        return None

    def _pull_request_issue_number(self, event: NormalizedEvent, signature: dict[str, str] | None) -> int | None:
        if signature and signature.get("issue"):
            issue_number = int(signature["issue"])
            if signature.get("job") and self.storage.get_job(signature["job"]) is not None:
                self.storage.update_job(signature["job"], status="completed", pr_number=event.number)
            return issue_number
        return self._extract_issue_number(event.body)

    def _queue_external_pull_request_review(
        self,
        repo: RepoConfig,
        event: NormalizedEvent,
        *,
        issue_number: int | None,
        untracked: bool = False,
    ) -> dict[str, Any]:
        event_key = self._external_pull_request_event_key(event)
        if not self.storage.record_processed_event(event_key):
            return {"status": "ignored", "reason": "duplicate_external_pr_event"}

        consumed_review_rounds = self._consumed_external_review_rounds(event.repo_full_name, event.number)
        review_round_cap = 1 if untracked else self.config.review_rounds

        if len(consumed_review_rounds) >= review_round_cap:
            reason = "untracked_external_review_round_consumed" if untracked else "external_review_rounds_exhausted"
            return {"status": "ignored", "reason": reason}

        next_review_round = max(consumed_review_rounds, default=0) + 1
        metadata: dict[str, Any] = {
            "title": event.title or "",
            "body": event.body or "",
            **self._external_pr_metadata(event),
        }
        if untracked:
            metadata["untracked"] = True
        job = self._enqueue_job(
            repo,
            stage="review_round",
            issue_number=issue_number,
            pr_number=event.number,
            review_round=next_review_round,
            metadata=metadata,
        )
        return {"status": "queued", "job_id": job.id, "stage": job.stage}

    def _external_contributor_account_too_new(self, event: NormalizedEvent) -> bool:
        created_at = self._external_contributor_created_at(event)
        if not created_at:
            return False
        try:
            account_created_at = self._parse_github_timestamp(created_at)
        except ValueError:
            logger.warning(
                "Unable to parse external contributor account creation date",
                extra={"repo_full_name": event.repo_full_name, "pr_number": event.number, "created_at": created_at},
            )
            return False
        return datetime.now(tz=timezone.utc) - account_created_at < MIN_EXTERNAL_CONTRIBUTOR_ACCOUNT_AGE

    def _external_contributor_created_at(self, event: NormalizedEvent) -> str | None:
        pull_request = event.payload.get("pull_request") or {}
        user = pull_request.get("user") or {}
        if isinstance(user, dict):
            created_at = user.get("created_at")
            if created_at:
                return str(created_at)
        login = self._external_contributor_login(event)
        if not login:
            return None
        try:
            github_user = self.github.get_user(login)
        except Exception:
            logger.warning(
                "Unable to fetch external contributor account metadata",
                extra={"repo_full_name": event.repo_full_name, "pr_number": event.number, "login": login},
                exc_info=True,
            )
            return None
        created_at = github_user.get("created_at") if isinstance(github_user, dict) else None
        return str(created_at) if created_at else None

    def _external_contributor_login(self, event: NormalizedEvent) -> str | None:
        pull_request = event.payload.get("pull_request") or {}
        user = pull_request.get("user") or {}
        if isinstance(user, dict) and user.get("login"):
            return str(user["login"])
        return event.actor_login or None

    def _parse_github_timestamp(self, value: str) -> datetime:
        normalized_value = value.strip()
        if normalized_value.endswith("Z"):
            normalized_value = f"{normalized_value[:-1]}+00:00"
        parsed = datetime.fromisoformat(normalized_value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _close_ineligible_external_pull_request(self, event: NormalizedEvent) -> dict[str, Any]:
        self.github.create_pr_comment(event.repo_full_name, event.number, INELIGIBLE_EXTERNAL_PR_COMMENT)
        self.github.close_pull_request(event.repo_full_name, event.number)
        return {"status": "closed", "reason": "contributor_account_too_new", "pr_number": event.number}

    def _agent_event_key(self, signature: dict[str, str], *, default_pr: int | None = None) -> str:
        fields = [("stage", signature.get("stage", ""))]
        for key in ("pr", "round", "job", "verdict"):
            value = signature.get(key)
            if key == "pr" and not value and default_pr is not None:
                value = str(default_pr)
            if value:
                fields.append((key, value))
        return ";".join(f"{key}={value}" for key, value in fields)

    def _external_pull_request_event_key(self, event: NormalizedEvent) -> str:
        if event.delivery_id:
            return f"external_pr_event;delivery={event.delivery_id}"

        fields = [
            ("repo", event.repo_full_name),
            ("kind", event.kind),
            ("pr", str(event.number)),
            ("action", event.action),
        ]
        if event.commit_sha:
            fields.append(("head_sha", event.commit_sha))
        elif event.head_branch:
            fields.append(("head_branch", event.head_branch))
        requested_reviewer = event.payload.get("requested_reviewer") or {}
        reviewer_login = requested_reviewer.get("login")
        if reviewer_login:
            fields.append(("reviewer", str(reviewer_login)))
        requested_team = event.payload.get("requested_team") or {}
        team_slug = requested_team.get("slug") or requested_team.get("name")
        if team_slug:
            fields.append(("team", str(team_slug)))
        updated_at = (event.payload.get("pull_request") or {}).get("updated_at")
        if updated_at:
            fields.append(("updated_at", str(updated_at)))
        return ";".join(f"{key}={value}" for key, value in fields)

    def _latest_review_round(self, repo_full_name: str, pr_number: int) -> int:
        rounds = [
            int(job.review_round or 0)
            for job in self.storage.find_jobs(repo_full_name=repo_full_name, stage="review_round", pr_number=pr_number)
        ]
        return max(rounds, default=0)

    def _consumed_external_review_rounds(self, repo_full_name: str, pr_number: int) -> set[int]:
        return {
            int(job.review_round)
            for job in self.storage.find_jobs(repo_full_name=repo_full_name, stage="review_round", pr_number=pr_number)
            if (
                job.metadata.get("external_contribution")
                and job.review_round is not None
                and job.status in {"queued", "launched", "completed"}
            )
        }

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

    def _is_pr_open(self, repo_full_name: str, pr_number: int) -> bool:
        pull_request = self.github.get_pull_request(repo_full_name, pr_number)
        return pull_request.get("state") == "open"

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

    def _render_issue_discussion(self, repo_full_name: str, issue_number: int, *, limit: int = 8) -> str:
        comments = self.github.issue_comments(repo_full_name, issue_number)
        rendered: list[str] = []
        for comment in comments[-limit:]:
            body = str(comment.get("body") or "").strip()
            if not body:
                continue
            author = comment.get("user", {}).get("login") or comment.get("author", {}).get("login") or "unknown"
            rendered.append(f"[{author}]\n{body}")
        return "\n\n".join(rendered)

    def _is_rollout_missing_error(self, exc: Exception) -> bool:
        if isinstance(exc, RolloutMissingError):
            return True
        error_text = str(exc)
        return any(
            pattern.search(error_text) for pattern in (*ROLLOUT_MISSING_PATTERNS, *OPENCODE_SESSION_MISSING_PATTERNS)
        )

    def _post_session_lost_warning(self, job: JobRecord) -> None:
        if job.stage != "issue_followup" or job.issue_number is None:
            return
        event_key = f"repo={job.repo_full_name};stage=session_lost;issue={job.issue_number}"
        signature = build_signature(stage="session_lost", issue=job.issue_number)
        if self.github.find_comments_by_signature(
            job.repo_full_name,
            job.issue_number,
            kind="issue",
            signature_fragment=signature,
        ):
            if not self.storage.has_processed_event(event_key):
                self.storage.record_processed_event(event_key)
            return
        if self.storage.has_processed_event(event_key):
            return
        body = (
            "⚠️ dani 세션 기록이 유실되어 이전 대화를 이어갈 수 없습니다. "
            f"`dani restart-issue {job.repo_full_name} {job.issue_number}` 로 새 세션을 시작해 주세요.\n\n"
            f"{signature}"
        )
        self.github.create_issue_comment(job.repo_full_name, job.issue_number, body)
        self.storage.record_processed_event(event_key)
