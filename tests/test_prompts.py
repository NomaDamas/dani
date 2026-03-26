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
            "signature": "<!-- dani:stage=issue_request;job=abc;issue=7 -->",
        },
    )

    assert "AI-understood issue summary" in prompt
    assert "Expected Outcome" in prompt


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
