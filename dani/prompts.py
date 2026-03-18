from __future__ import annotations

from string import Template
from typing import Any

from dani.signatures import build_signature

TEMPLATES = {
    "issue_request": Template(
        """
You are operating inside repository: $repo
Local path: $local_path
Task: review GitHub issue #$issue_number titled "$issue_title".

Issue body:
$issue_body

Write a GitHub issue comment via gh CLI that includes:
1. Why this issue is needed
2. Why this issue may not be needed
3. A concise implementation plan
4. Agent Signature

Use this exact signature somewhere in the comment:
$signature

After posting the comment, exit.
        """.strip()
    ),
    "implementation": Template(
        """
You are operating inside repository: $repo
Local path: $local_path
Issue #$issue_number: $issue_title

Issue body:
$issue_body

Discussion context:
$discussion

Implement the approved change.
Requirements:
- Use $$ralph to finish the work
- Write tests first (TDD)
- Make all tests pass
- Actually run the code and verify behavior
- Create/update branch named like feature/#$issue_number
- Open a PR targeting $dev_branch
- Put this signature in the PR body:
$signature

After creating the PR with gh CLI, exit.
        """.strip()
    ),
    "review_round": Template(
        """
You are reviewing PR #$pr_number in $repo.
Round: $round_number / 3
PR title: $pr_title
PR body:
$pr_body

Recent discussion:
$discussion

Use the code locally, review the changes, and leave exactly one GitHub PR comment summarizing the review findings.
Include this exact signature in the comment:
$signature

After posting the PR comment with gh CLI, exit.
        """.strip()
    ),
    "final_verdict": Template(
        """
You are deciding the final verdict for PR #$pr_number in $repo.
PR title: $pr_title
PR body:
$pr_body

Review history:
$discussion

Leave exactly one final GitHub PR comment with APPROVE or REJECT and a short reason.
If you approve, include this exact signature in the comment:
$approve_signature
If you reject, include this exact signature in the comment:
$reject_signature

After posting the PR comment with gh CLI, exit.
        """.strip()
    ),
}


def render_prompt(template_name: str, context: dict[str, Any]) -> str:
    template = TEMPLATES[template_name]
    if template_name != "final_verdict" and "signature" not in context:
        context = {
            **context,
            "signature": build_signature(stage=template_name, job_id=context.get("job_id", "unknown")),
        }
    return template.substitute({key: "" if value is None else str(value) for key, value in context.items()})
