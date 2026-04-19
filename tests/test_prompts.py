from dani.prompts import render_prompt


def test_implementation_prompt_keeps_ralph_literal() -> None:
    prompt = render_prompt(
        "implementation",
        {
            "repo": "acme/demo",
            "local_path": "workspace/demo",
            "issue_number": 7,
            "issue_title": "Need a bot",
            "issue_body": "Implement it",
            "discussion": "approved",
            "pr_context": "",
            "pr_number": "",
            "dev_branch": "dev",
            "signature": "<!-- dani:stage=implementation;job=abc;issue=7 -->",
            "signature_instructions": "Use this signature in the PR body:\n<!-- dani:stage=implementation;job=abc;issue=7 -->",
        },
    )

    assert "$ralph" in prompt
    assert "<!-- dani:stage=implementation;job=abc;issue=7 -->" in prompt


def test_implementation_prompt_for_omo_replaces_ralph_with_ultrawork() -> None:
    prompt = render_prompt(
        "implementation",
        {
            "repo": "acme/demo",
            "local_path": "workspace/demo",
            "issue_number": 7,
            "issue_title": "Need a bot",
            "issue_body": "Implement it",
            "discussion": "approved",
            "pr_context": "",
            "pr_number": "",
            "dev_branch": "dev",
            "signature": "<!-- dani:stage=implementation;job=abc;issue=7 -->",
            "signature_instructions": "Use this signature in the PR body:\n<!-- dani:stage=implementation;job=abc;issue=7 -->",
        },
        runtime="omo",
    )

    assert "$ralph" not in prompt
    assert "ultrawork" in prompt


def test_implementation_prompt_for_omx_explicit_runtime_still_keeps_ralph() -> None:
    prompt = render_prompt(
        "implementation",
        {
            "repo": "acme/demo",
            "local_path": "workspace/demo",
            "issue_number": 7,
            "issue_title": "Need a bot",
            "issue_body": "Implement it",
            "discussion": "approved",
            "pr_context": "",
            "pr_number": "",
            "dev_branch": "dev",
            "signature": "<!-- dani:stage=implementation;job=abc;issue=7 -->",
            "signature_instructions": "Use this signature in the PR body:\n<!-- dani:stage=implementation;job=abc;issue=7 -->",
        },
        runtime="omx",
    )

    assert "$ralph" in prompt


def test_implementation_prompt_prefers_push_over_pr_edit_for_existing_pr() -> None:
    prompt = render_prompt(
        "implementation",
        {
            "repo": "acme/demo",
            "local_path": "workspace/demo",
            "issue_number": 7,
            "issue_title": "Need a bot",
            "issue_body": "Implement it",
            "discussion": "approved",
            "pr_context": "",
            "pr_number": "",
            "dev_branch": "dev",
            "signature": "<!-- dani:stage=implementation;job=abc;issue=7 -->",
            "signature_instructions": "Use this signature in the PR body:\n<!-- dani:stage=implementation;job=abc;issue=7 -->",
        },
    )

    assert "push new commits to the same branch so the PR updates automatically" in prompt
    assert "gh pr edit" not in prompt


def test_implementation_prompt_for_existing_pr_requires_signed_followup_comment() -> None:
    prompt = render_prompt(
        "implementation",
        {
            "repo": "acme/demo",
            "local_path": "workspace/demo",
            "issue_number": 7,
            "issue_title": "Need a bot",
            "issue_body": "Implement it",
            "discussion": "approved",
            "pr_context": "Existing PR context:\nPR #39\n\nPR review/comment history to address:\nPlease fix the failing case.",
            "pr_number": 39,
            "dev_branch": "dev",
            "signature": "<!-- dani:stage=implementation;job=abc;issue=7;pr=39 -->",
            "signature_instructions": "Post it with:\n gh pr comment 39 --repo acme/demo --body-file <implementation-update.md>",
        },
    )

    assert "Existing PR context" in prompt
    assert "Please fix the failing case." in prompt
    assert "gh pr comment 39 --repo acme/demo --body-file <implementation-update.md>" in prompt


def test_issue_request_prompt_uses_gh_instructions() -> None:
    prompt = render_prompt(
        "issue_request",
        {
            "repo": "acme/demo",
            "local_path": "workspace/demo",
            "issue_number": 7,
            "issue_title": "Need a bot",
            "issue_body": "Implement it",
            "discussion": "",
            "signature": "<!-- dani:stage=issue_request;job=abc;issue=7 -->",
        },
    )

    assert "gh issue comment 7 --repo acme/demo --body-file <comment-file.md>" in prompt
    assert "PyGithub helper" not in prompt


def test_issue_request_prompt_requires_ai_summary_and_expected_outcome() -> None:
    prompt = render_prompt(
        "issue_request",
        {
            "repo": "acme/demo",
            "local_path": "workspace/demo",
            "issue_number": 7,
            "issue_title": "Need a bot",
            "issue_body": "Implement it",
            "discussion": "",
            "signature": "<!-- dani:stage=issue_request;job=abc;issue=7 -->",
        },
    )

    assert "AI-understood issue summary" in prompt
    assert "Expected Outcome" in prompt


def test_issue_request_prompt_demands_evidence_based_plan() -> None:
    prompt = render_prompt(
        "issue_request",
        {
            "repo": "acme/demo",
            "local_path": "workspace/demo",
            "issue_number": 7,
            "issue_title": "Need a bot",
            "issue_body": "Implement it",
            "discussion": "",
            "signature": "<!-- dani:stage=issue_request;job=abc;issue=7 -->",
        },
    )

    assert "Evidence-based implementation plan" in prompt
    assert "Concise implementation plan" not in prompt


def test_issue_request_prompt_instructs_research_before_planning() -> None:
    prompt = render_prompt(
        "issue_request",
        {
            "repo": "acme/demo",
            "local_path": "workspace/demo",
            "issue_number": 7,
            "issue_title": "Need a bot",
            "issue_body": "Implement it",
            "discussion": "",
            "signature": "<!-- dani:stage=issue_request;job=abc;issue=7 -->",
        },
    )

    lowered = prompt.lower()
    assert "search the codebase" in lowered or "explore the codebase" in lowered
    assert "external" in lowered
    assert "official docs" in lowered or "documentation" in lowered


def test_issue_request_prompt_allows_explicit_not_found_statement() -> None:
    prompt = render_prompt(
        "issue_request",
        {
            "repo": "acme/demo",
            "local_path": "workspace/demo",
            "issue_number": 7,
            "issue_title": "Need a bot",
            "issue_body": "Implement it",
            "discussion": "",
            "signature": "<!-- dani:stage=issue_request;job=abc;issue=7 -->",
        },
    )

    lowered = prompt.lower()
    assert "no existing reusable code found" in lowered
    assert "no suitable external library found" in lowered


def test_issue_request_prompt_specifies_citation_format() -> None:
    prompt = render_prompt(
        "issue_request",
        {
            "repo": "acme/demo",
            "local_path": "workspace/demo",
            "issue_number": 7,
            "issue_title": "Need a bot",
            "issue_body": "Implement it",
            "discussion": "",
            "signature": "<!-- dani:stage=issue_request;job=abc;issue=7 -->",
        },
    )

    assert "path/to/file.py:line" in prompt
    assert "URL" in prompt


def test_issue_request_prompt_includes_existing_discussion_history() -> None:
    prompt = render_prompt(
        "issue_request",
        {
            "repo": "acme/demo",
            "local_path": "workspace/demo",
            "issue_number": 7,
            "issue_title": "Need a bot",
            "issue_body": "Implement it",
            "discussion": "[human]\nEarlier context",
            "signature": "<!-- dani:stage=issue_request;job=abc;issue=7 -->",
        },
    )

    assert "Existing issue discussion history" in prompt
    assert "Earlier context" in prompt


def test_review_round_prompt_requires_code_review_and_verification() -> None:
    prompt = render_prompt(
        "review_round",
        {
            "repo": "acme/demo",
            "pr_number": 5,
            "pr_title": "Feature",
            "pr_body": "Body",
            "discussion": "history",
            "round_number": 2,
            "signature": "<!-- dani:stage=review_round;job=abc;pr=5;round=2 -->",
        },
    )

    assert "$code-review" in prompt
    assert "actual verification" in prompt.lower()
    assert "concrete evidence appropriate for what you verified" in prompt
    assert "gh pr comment 5 --repo acme/demo --body-file <review-comment.md>" in prompt


def test_review_round_prompt_for_omo_delegates_to_momus_plan_critic() -> None:
    prompt = render_prompt(
        "review_round",
        {
            "repo": "acme/demo",
            "pr_number": 5,
            "pr_title": "Feature",
            "pr_body": "Body",
            "discussion": "history",
            "round_number": 2,
            "signature": "<!-- dani:stage=review_round;job=abc;pr=5;round=2 -->",
        },
        runtime="omo",
    )

    assert "$code-review" not in prompt
    assert "Momus-Plan-Critic" in prompt
    assert "subagent" in prompt.lower()


def test_review_round_prompt_for_omo_does_not_mention_ralph_command() -> None:
    prompt = render_prompt(
        "review_round",
        {
            "repo": "acme/demo",
            "pr_number": 5,
            "pr_title": "Feature",
            "pr_body": "Body",
            "discussion": "history",
            "round_number": 2,
            "signature": "<!-- dani:stage=review_round;job=abc;pr=5;round=2 -->",
        },
        runtime="omo",
    )

    assert "$ralph" not in prompt


def test_review_round_prompt_for_external_contribution_mentions_contributor_ownership() -> None:
    prompt = render_prompt(
        "review_round",
        {
            "repo": "acme/demo",
            "pr_number": 5,
            "pr_title": "Feature",
            "pr_body": "Body",
            "discussion": "history",
            "round_number": 2,
            "round_total": 10,
            "review_mode_note": (
                "This is an external contribution PR. The contributor owns any follow-up implementation. "
                "If the PR is ready to merge, include /approve in your review comment so dani can run the final verdict pass."
            ),
            "signature": "<!-- dani:stage=review_round;job=abc;pr=5;round=2 -->",
        },
    )

    assert "2 / 10" in prompt
    assert "external contribution pr" in prompt.lower()
    assert "/approve" in prompt


def test_final_verdict_prompt_contains_both_signatures() -> None:
    prompt = render_prompt(
        "final_verdict",
        {
            "repo": "acme/demo",
            "pr_number": 5,
            "pr_title": "Feature",
            "pr_body": "Body",
            "discussion": "history",
            "approve_signature": "<!-- dani:stage=final_verdict;job=abc;pr=5;verdict=APPROVE -->",
            "reject_signature": "<!-- dani:stage=final_verdict;job=abc;pr=5;verdict=REJECT -->",
        },
    )

    assert "verdict=APPROVE" in prompt
    assert "verdict=REJECT" in prompt


def test_final_verdict_prompt_requires_general_real_result_evidence() -> None:
    prompt = render_prompt(
        "final_verdict",
        {
            "repo": "acme/demo",
            "pr_number": 5,
            "pr_title": "Feature",
            "pr_body": "Body",
            "discussion": "history",
            "approve_signature": "<!-- dani:stage=final_verdict;job=abc;pr=5;verdict=APPROVE -->",
            "reject_signature": "<!-- dani:stage=final_verdict;job=abc;pr=5;verdict=REJECT -->",
        },
    )

    assert "real result from actual verification" in prompt.lower()
    assert "concrete evidence appropriate for what you verified" in prompt
    assert "gh pr comment 5 --repo acme/demo --body-file <final-verdict.md>" in prompt
    assert "web:" not in prompt.lower()
    assert "cli:" not in prompt.lower()
    assert "backend:" not in prompt.lower()


def test_merge_conflict_resolution_prompt_requires_recheck_without_direct_merge() -> None:
    prompt = render_prompt(
        "merge_conflict_resolution",
        {
            "repo": "acme/demo",
            "local_path": "workspace/demo",
            "issue_number": 7,
            "pr_number": 5,
            "pr_title": "Feature",
            "pr_body": "Body",
            "head_branch": "Feature/#7",
            "base_branch": "dev",
            "conflict_reason": "merge conflict with base branch",
            "signature": "<!-- dani:stage=merge_conflict_resolution;job=abc;pr=5 -->",
        },
    )

    assert "rerun the final verdict" in prompt
    assert "Do not merge the PR yourself" in prompt
    assert "stage=merge_conflict_resolution" in prompt
    assert "gh pr comment 5 --repo acme/demo --body-file <merge-conflict-comment.md>" in prompt


def test_human_escalation_prompt_mentions_review_limit_and_signature() -> None:
    prompt = render_prompt(
        "human_escalation",
        {
            "repo": "acme/demo",
            "pr_number": 5,
            "pr_title": "Feature",
            "pr_body": "Body",
            "discussion": "history",
            "review_limit": 10,
            "signature": "<!-- dani:stage=human_escalation;job=abc;pr=5 -->",
        },
    )

    assert "10" in prompt
    assert "human maintainer" in prompt.lower()
    assert "gh pr comment 5 --repo acme/demo --body-file <human-escalation.md>" in prompt
    assert "stage=human_escalation" in prompt
