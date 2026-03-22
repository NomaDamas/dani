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

Write one GitHub issue comment.
Checklist:
- [ ] AI-understood issue summary
- [ ] Why this issue is needed
- [ ] Why this issue may not be needed
- [ ] Expected Outcome
- [ ] Concise implementation plan
- [ ] Agent Signature

Use this exact signature somewhere in the comment:
$signature

Post it with gh (write the comment to a file first, then send it):
gh issue comment $issue_number --repo $repo --body-file <comment-file.md>

After posting the comment, exit.
        """.strip()
    ),
    "issue_followup": Template(
        """
You are resuming the existing discussion for GitHub issue #$issue_number in $repo.
Local path: $local_path
Issue title: $issue_title

Original issue body:
$issue_body

New user follow-up comment:
$comment_body

Continue the existing issue discussion instead of restarting the analysis from scratch.
Write exactly one GitHub issue comment that addresses the new follow-up and includes this exact signature:
$signature

Post it with gh (write the comment to a file first, then send it):
gh issue comment $issue_number --repo $repo --body-file <followup-comment.md>

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
- Commit and push your changes to feature/#$issue_number
- Ensure there is a PR targeting $dev_branch for feature/#$issue_number
  - If no PR exists, create it with:
    gh pr create --repo $repo --head feature/#$issue_number --base $dev_branch --title "Feature/#$issue_number" --body-file <pr-body.md>
  - If a PR already exists, push new commits to the same branch so the PR updates automatically
  - Update the PR body only if needed to keep the description/signature accurate
  - Leave a PR comment if needed to summarize what changed in this update
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

Use the code locally and run $$code-review before writing the review comment.
Do real verification, not only static inspection.
Checklist:
- [ ] Use $$code-review
- [ ] Run the code or tests needed to validate behavior
- [ ] Include Real Result from actual verification
- [ ] Include concrete evidence appropriate for what you verified
- [ ] Include this exact signature: $signature

Post it with gh:
gh pr comment $pr_number --repo $repo --body-file <review-comment.md>

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

Leave exactly one final GitHub PR comment.
Checklist:
- [ ] Verdict: APPROVE or REJECT
- [ ] Short reason
- [ ] Real Result from actual verification
- [ ] Include concrete evidence appropriate for what you verified
- [ ] If APPROVE, include: $approve_signature
- [ ] If REJECT, include: $reject_signature

Post it with gh:
gh pr comment $pr_number --repo $repo --body-file <final-verdict.md>

After posting the PR comment, exit.
        """.strip()
    ),
}


def render_prompt(template_name: str, context: dict[str, Any]) -> str:
    template = TEMPLATES[template_name]
    context = dict(context)
    if template_name != "final_verdict" and "signature" not in context:
        context = {
            **context,
            "signature": build_signature(stage=template_name, job_id=context.get("job_id", "unknown")),
        }
    return template.substitute({key: "" if value is None else str(value) for key, value in context.items()})
