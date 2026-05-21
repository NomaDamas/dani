from __future__ import annotations

from string import Template
from typing import Any

from dani.signatures import build_signature

# Non-interactive automation guard.
#
# dani drives agents in a non-interactive webhook-triggered loop. There is no
# human attached to the session: any tool that waits for human input (notably
# opencode's `question` tool) will block the session forever, eventually
# tripping `agent_timeout_seconds` and failing the job. omx (codex) does not
# expose `question` today, but the guard is run-time-agnostic so future omx
# tool surface changes can't reintroduce the same stall.
NON_INTERACTIVE_GUARD = (
    "NON-INTERACTIVE AUTOMATION CONTRACT (read first):\n"
    "- You are running inside dani's non-interactive automation loop. There is NO human attached to this session.\n"
    "- DO NOT call the `question` tool, DO NOT ask the user for clarification, DO NOT request approval mid-task. Any tool that waits for a human reply will hang the session until it is force-killed by timeout, and the job will be marked failed.\n"
    "- If scope or intent is ambiguous, pick the most conservative reasonable interpretation, state your assumption explicitly in the comment/PR you post, and proceed. Do not stop to ask.\n"
    "- If a blocker is genuinely unresolvable (missing credential, destructive action you must not take, etc.), document it inline in the comment/PR body you post and exit normally. Do not call `question` to surface it.\n"
)


TEMPLATES = {
    "issue_request": Template(
        """
You are operating inside repository: $repo
Local path: $local_path
Task: review GitHub issue #$issue_number titled "$issue_title".

ROLE: PLANNING AGENT (read-only analysis, no implementation).
- You analyze and propose a plan. You DO NOT write code, create branches, or open PRs in this session.
- After you post the comment, your session ENDS. dani discards your in-memory state.
- Implementation only starts when a human responds with a comment containing "/approve". At that point dani spawns a NEW, SEPARATE agent session in a fresh process. That agent does not inherit your reasoning trace — it only sees the issue body and the GitHub discussion.
- Do NOT promise to "create a PR", "open a branch", "write the code", "push commits", or "do the work next". Phrase the plan as "the implementation agent will...".
- Your comment is the entire handoff. Make it self-contained: anything the implementation agent must know has to be IN the comment text.

Issue body:
$issue_body

Existing issue discussion history:
$discussion

Before writing the comment, do real research — do not produce a plan from memory alone.
Research requirements:
- Search the codebase for existing reusable code (modules, functions, utilities, patterns) that already address part of the issue. Cite any findings as path/to/file.py:line.
- Investigate external sources (official docs, GitHub repos, package registries, web search) for APIs or libraries that already provide the needed capability. Cite any findings as "Title - URL".
- If nothing is reusable in-repo, state exactly: "no existing reusable code found".
- If no suitable external dependency exists, state exactly: "no suitable external library found".

Write one GitHub issue comment.
Checklist:
- [ ] AI-understood issue summary
- [ ] Why this issue is needed
- [ ] Why this issue may not be needed
- [ ] Expected Outcome
- [ ] Evidence-based implementation plan (phrased as instructions for the implementation agent)
- [ ] Assumptions / human decisions to resolve asynchronously before /approve (if any)
- [ ] Reminder that implementation starts only after a human comment containing "/approve"
- [ ] Agent Signature

For the "Evidence-based implementation plan" section, report:
- Feasibility grounded in the research above (reusable code and/or external libraries, with citations).
- A concrete step-by-step plan that references the cited evidence (path/to/file.py:line for code, "Title - URL" for external references).
- Any risks or assumptions surfaced by the research.

Use this exact signature somewhere in the comment:
$signature

POST EXACTLY ONCE. Strict anti-duplicate contract:
- Before calling `gh issue comment`, run:
  gh issue view $issue_number --repo $repo --json comments --jq '.comments[].body' | grep -F '$signature' || true
  If that command prints anything non-empty, a comment with this exact signature already exists. DO NOT post again. Exit immediately.
- Call `gh issue comment` at most ONE time in this session.
- After `gh issue comment` returns successfully, do not run it again under any circumstance — not for retries, not for self-review, not for "double-checking". Exit.
- If `gh issue comment` appears to fail, re-run the `gh issue view ... | grep` check before retrying. If the signature is already present, the post succeeded; do not retry; exit.

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

ROLE: PLANNING AGENT (read-only analysis, no implementation).
- You refine the plan based on the new comment. You DO NOT write code, create branches, or open PRs in this session.
- After you post the comment, your session ENDS. dani discards your in-memory state.
- Implementation only starts when a human responds with a comment containing "/approve". At that point dani spawns a NEW, SEPARATE agent session in a fresh process. That agent does not inherit your reasoning trace — it only sees the issue body and the GitHub discussion.
- Do NOT promise to "create a PR", "open a branch", "write the code", "push commits", or "do the work next". Phrase next steps as "the implementation agent will...".
- Your comment is the entire handoff. Make it self-contained: anything the implementation agent must know has to be IN the comment text or in earlier visible discussion.

Original issue body:
$issue_body

New user follow-up comment:
$comment_body

Continue the existing issue discussion instead of restarting the analysis from scratch.
Write exactly one GitHub issue comment that addresses the new follow-up. The comment must:
- Answer or clarify the user's follow-up directly.
- Update the implementation plan if the follow-up changes scope/approach.
- Note any assumptions / human decisions to resolve asynchronously before "/approve".
- Remind the human that implementation starts only after a comment containing "/approve".
- Include this exact signature on its own line:
$signature

POST EXACTLY ONCE. Strict anti-duplicate contract:
- Before calling `gh issue comment`, run:
  gh issue view $issue_number --repo $repo --json comments --jq '.comments[].body' | grep -F '$signature' || true
  If that command prints anything non-empty, a comment with this exact signature already exists. DO NOT post again. Exit immediately.
- Call `gh issue comment` at most ONE time in this session.
- After `gh issue comment` returns successfully, do not run it again under any circumstance — not for retries, not for self-review, not for "double-checking". Exit.
- If `gh issue comment` appears to fail, re-run the `gh issue view ... | grep` check before retrying. If the signature is already present, the post succeeded; do not retry; exit.

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

$pr_context

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
$signature_instructions

After creating or updating the PR, exit.
        """.strip()
    ),
    "review_round": Template(
        """
You are reviewing PR #$pr_number in $repo.
Round: $round_number / $round_total
PR title: $pr_title
PR body:
$pr_body

Recent discussion:
$discussion

$review_mode_note
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
gh pr comment $pr_number --repo $repo --body-file <merge-conflict-comment.md>

After posting the PR comment, exit.
        """.strip()
    ),
    "final_verdict": Template(
        """
You are deciding the final verdict for PR #$pr_number in $repo.
$review_cycle
PR title: $pr_title
PR body:
$pr_body

Review history:
$discussion

Leave exactly one GitHub PR comment for this review pass.
Checklist:
- [ ] Verdict: APPROVE or REJECT
- [ ] Short reason
- [ ] Real Result from actual verification
- [ ] Include concrete evidence appropriate for what you verified
- [ ] If REJECT, make the next contributor action clear
- [ ] If APPROVE, include: $approve_signature
- [ ] If REJECT, include: $reject_signature

Post it with gh:
gh pr comment $pr_number --repo $repo --body-file <final-verdict.md>

After posting the PR comment, exit.
        """.strip()
    ),
    "dev_sync_conflict": Template(
        """
You are operating inside repository: $repo
Local path: $local_path

The worktree is already in a merge-conflict state.
Goal: merge $main_branch commit $main_sha into $dev_branch and push directly to origin/$dev_branch.
Temporary branch: $temp_branch

Requirements:
- Resolve every existing merge conflict in this worktree
- Preserve intended behavior from both branches unless the codebase clearly indicates otherwise
- Run the smallest relevant verification needed for the files you changed
- Do not open a PR
- Commit the resolved merge using this exact commit message:

$commit_message

- Push the resolved merge with:
  git push origin HEAD:refs/heads/$dev_branch
- Before exiting, make sure there are no unmerged files left:
  git diff --name-only --diff-filter=U

After the push succeeds, exit.
        """.strip()
    ),
}


# omx uses codex shell-slash commands ($ralph, $code-review); omo (opencode)
# has neither, so the equivalent intents are swapped in post-render: ralph loop
# -> ultrawork mode (always-on for omo), $code-review -> Momus-Plan-Critic subagent.
_RUNTIME_SUBSTITUTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "omo": (
        ("$code-review", "the Momus-Plan-Critic subagent for rigorous verification"),
        ("$ralph", "ultrawork"),
    ),
}


def ensure_non_interactive_guard(prompt: str) -> str:
    """Return *prompt* with the non-interactive guard at the very top, once."""
    if prompt.startswith(NON_INTERACTIVE_GUARD):
        return prompt
    return f"{NON_INTERACTIVE_GUARD}\n{prompt}"


def split_non_interactive_guard(prompt: str) -> str:
    """Return the prompt body after the non-interactive guard, if present."""
    if not prompt.startswith(NON_INTERACTIVE_GUARD):
        return prompt
    return prompt.removeprefix(NON_INTERACTIVE_GUARD).lstrip("\n")


def render_prompt(template_name: str, context: dict[str, Any], *, runtime: str = "omx") -> str:
    template = TEMPLATES[template_name]
    context = dict(context)
    context.setdefault("review_cycle", "")
    context.setdefault("round_total", "3")
    context.setdefault("review_mode_note", "")
    if template_name != "final_verdict" and "signature" not in context:
        context = {
            **context,
            "signature": build_signature(stage=template_name, job_id=context.get("job_id", "unknown")),
        }
    rendered = template.substitute({key: "" if value is None else str(value) for key, value in context.items()})
    substitutions = _RUNTIME_SUBSTITUTIONS.get((runtime or "omx").strip().lower())
    if substitutions:
        for needle, replacement in substitutions:
            rendered = rendered.replace(needle, replacement)
    return ensure_non_interactive_guard(rendered)
