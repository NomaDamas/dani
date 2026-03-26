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

Write a GitHub issue comment that includes:
1. Why this issue is needed
2. Why this issue may not be needed
3. A concise implementation plan
4. Agent Signature

Use this exact signature somewhere in the comment:
$signature

Post it with the bundled PyGithub helper (write the comment to a file first, then send it):
$github_helper issue-comment --repo $repo --issue $issue_number --body-file <comment-file.md>

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
- Open or update a PR targeting $dev_branch with the bundled PyGithub helper:
  $github_helper ensure-pr --repo $repo --head feature/#$issue_number --base $dev_branch --title "Feature/#$issue_number" --body-file <pr-body.md>
- Put this signature in the PR body:
$signature

After creating or updating the PR, exit.
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

Post it with the bundled PyGithub helper:
$github_helper pr-comment --repo $repo --pr $pr_number --body-file <review-comment.md>

After posting the PR comment, exit.
        """.strip()
    ),
    "merge_conflict_resolution": Template(
        """
You are resolving a merge conflict for PR #$pr_number in $repo.
Local path: $local_path
PR title: $pr_title
PR body:
$pr_body

Related issue: #$issue_number
Head branch: $head_branch
Base branch: $base_branch
Conflict reason:
$conflict_reason

Resolve the merge conflict so the PR can be reviewed again safely.
Requirements:
- Fetch the latest remote branches
- Check out the PR head branch locally
- Update the head branch from $base_branch and resolve every merge conflict
- Re-run the relevant tests/verification after the merge update
- Push the resolved branch back to the remote
- Leave exactly one GitHub PR comment summarizing what changed and what you verified
- Include this exact signature in the comment:
$signature
- Do not merge the PR yourself; dani will rerun the final verdict after your comment

Post it with the bundled PyGithub helper:
$github_helper pr-comment --repo $repo --pr $pr_number --body-file <merge-conflict-comment.md>

After posting the PR comment, exit.
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

Post it with the bundled PyGithub helper:
$github_helper pr-comment --repo $repo --pr $pr_number --body-file <final-verdict.md>

After posting the PR comment, exit.
        """.strip()
    ),
}


def render_prompt(template_name: str, context: dict[str, Any]) -> str:
    template = TEMPLATES[template_name]
    context = {"github_helper": "", **context}
    if template_name != "final_verdict" and "signature" not in context:
        context = {
            **context,
            "signature": build_signature(stage=template_name, job_id=context.get("job_id", "unknown")),
        }
    return template.substitute({key: "" if value is None else str(value) for key, value in context.items()})
