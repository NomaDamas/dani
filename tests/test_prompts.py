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


def test_review_round_prompt_requires_real_result_evidence() -> None:
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

    assert "real result" in prompt.lower()
    assert "actual" in prompt.lower()


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


def test_final_verdict_prompt_requires_real_result_evidence() -> None:
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

    assert "real result" in prompt.lower()
    assert "screenshot" in prompt.lower()
    assert "cli" in prompt.lower()
    assert "api" in prompt.lower()
