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
