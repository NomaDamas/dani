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
            "dev_branch": "dev",
            "signature": "<!-- dani:stage=implementation;job=abc;issue=7 -->",
        },
    )

    assert "$ralph" in prompt
    assert "<!-- dani:stage=implementation;job=abc;issue=7 -->" in prompt


def test_issue_request_prompt_uses_pygithub_helper_instructions() -> None:
    prompt = render_prompt(
        "issue_request",
        {
            "repo": "acme/demo",
            "local_path": "workspace/demo",
            "issue_number": 7,
            "issue_title": "Need a bot",
            "issue_body": "Implement it",
            "signature": "<!-- dani:stage=issue_request;job=abc;issue=7 -->",
            "github_helper": "/usr/bin/python3 /tmp/dani/github_helper.py",
        },
    )

    assert "PyGithub helper" in prompt
    assert "/usr/bin/python3 /tmp/dani/github_helper.py" in prompt
    assert "gh CLI" not in prompt


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
