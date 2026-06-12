from __future__ import annotations

from typing import Protocol

from dani.agent_runner import normalize_runtime
from dani.models import DaniConfig, JobRecord, RepoConfig
from dani.prompts import NON_INTERACTIVE_GUARD, ensure_non_interactive_guard, split_non_interactive_guard
from dani.service_policy import ISSUE_COMMENT_RECOVERY_STAGES


class _PromptService(Protocol):
    config: DaniConfig

    def _issue_metadata(self, repo_full_name: str, issue_number: int) -> dict[str, str]: ...

    def _pull_request_metadata(self, repo_full_name: str, pr_number: int) -> dict[str, str]: ...

    def _build_issue_request_prompt(
        self, repo: RepoConfig, job: JobRecord, issue_number: int, issue_title: str, issue_body: str, runtime: str
    ) -> str: ...

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
    ) -> str: ...

    def _build_issue_followup_prompt(
        self, repo: RepoConfig, job: JobRecord, issue_number: int, issue_title: str, issue_body: str, runtime: str
    ) -> str: ...

    def _build_issue_comment_recovery_prompt(
        self, repo: RepoConfig, job: JobRecord, issue_number: int, issue_title: str, issue_body: str
    ) -> str: ...

    def _build_review_round_prompt(
        self,
        repo: RepoConfig,
        job: JobRecord,
        issue_number: int,
        pr_number: int,
        pr_title: str,
        pr_body: str,
        runtime: str,
    ) -> str: ...

    def _build_merge_conflict_resolution_prompt(
        self,
        repo: RepoConfig,
        job: JobRecord,
        issue_number: int,
        pr_number: int,
        pr_title: str,
        pr_body: str,
        runtime: str,
    ) -> str: ...

    def _build_final_verdict_prompt(
        self, job: JobRecord, issue_number: int, pr_number: int, pr_title: str, pr_body: str, runtime: str
    ) -> str: ...

    def _apply_prior_context(self, prompt: str, bridge_prompt: str) -> str: ...


class ServicePromptMixin:
    def _build_prompt(
        self: _PromptService,
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
        metadata_issue_title = job.metadata.get("title")
        issue_title = issue_context.get("title") or (
            metadata_issue_title if isinstance(metadata_issue_title, str) else f"Issue #{issue_number}"
        )
        metadata_issue_body = job.metadata.get("body")
        issue_body = issue_context.get("body") or (metadata_issue_body if isinstance(metadata_issue_body, str) else "")
        pr_snapshot = self._pull_request_metadata(repo.full_name, pr_number) if pr_number else {}
        metadata_pr_title = job.metadata.get("title")
        pr_title = pr_snapshot.get("title") or (
            metadata_pr_title if isinstance(metadata_pr_title, str) else f"PR #{pr_number}"
        )
        metadata_pr_body = job.metadata.get("body")
        pr_body = pr_snapshot.get("body") or (metadata_pr_body if isinstance(metadata_pr_body, str) else "")
        if job.stage == "issue_request":
            prompt = self._build_issue_request_prompt(
                repo, job, issue_number, issue_title, issue_body, resolved_runtime
            )
            return self._apply_prior_context(prompt, bridge_prompt)

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
            return self._apply_prior_context(prompt, bridge_prompt)

        if job.stage == "issue_followup":
            prompt = self._build_issue_followup_prompt(
                repo,
                job,
                issue_number,
                issue_title,
                issue_body,
                resolved_runtime,
            )
            return self._apply_prior_context(prompt, bridge_prompt)

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
            return self._apply_prior_context(prompt, bridge_prompt)

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
            return self._apply_prior_context(prompt, bridge_prompt)

        prompt = self._build_final_verdict_prompt(job, issue_number, pr_number, pr_title, pr_body, resolved_runtime)
        return self._apply_prior_context(prompt, bridge_prompt)

    def _apply_prior_context(self, prompt: str, bridge_prompt: str) -> str:
        guarded_prompt = ensure_non_interactive_guard(prompt)
        if not bridge_prompt.strip():
            return guarded_prompt
        bridge_block = (
            f"{bridge_prompt}\n\n"
            "Use the imported prior context above only as bounded background context. "
            "This is not a native resume; continue from it conservatively."
        )
        prompt_body = split_non_interactive_guard(guarded_prompt)
        return f"{NON_INTERACTIVE_GUARD}\n{bridge_block}\n\n{prompt_body}"
